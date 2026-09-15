"""日K 快照补 bar 逻辑单测。"""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest
from datetime import date
from unittest import mock

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import market as market_mod

_CST = dt.timezone(dt.timedelta(hours=8))


def setUpModule():
    clock = mock.patch("market_hours.now", return_value=dt.datetime(2026, 9, 15, 10, 30))
    clock.start()
    unittest.addModuleCleanup(clock.stop)


def _df_with_dates(dates, close=10.0):
    idx = pd.to_datetime(dates)
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 1000,
            "amount": 0.0,
        },
        index=idx,
    )


def _phase(value):
    return mock.patch("market_hours.session_phase", return_value=value)


class TestStripTodayBarDf(unittest.TestCase):
    def test_strip_when_last_is_today_in_session(self):
        today = market_mod.market_hours.now().date()
        yesterday = (pd.Timestamp(today) - pd.Timedelta(days=1)).date()
        df = _df_with_dates([yesterday.isoformat(), today.isoformat()], close=10.0)
        with mock.patch("market_hours.now") as mn:
            mn.return_value = pd.Timestamp(today)
            with _phase("trading"):
                out = market_mod._strip_today_bar_df(df)
        self.assertEqual(len(out), 1)
        self.assertEqual(market_mod._last_bar_date(out), yesterday)

    def test_strip_when_last_is_today_at_lunch_break(self):
        """午休 phase=break (in_session=False) 不得把不完整当日 bar 当终值落盘。"""
        today = market_mod.market_hours.now().date()
        yesterday = (pd.Timestamp(today) - pd.Timedelta(days=1)).date()
        df = _df_with_dates([yesterday.isoformat(), today.isoformat()], close=10.0)
        with mock.patch("market_hours.now") as mn:
            mn.return_value = pd.Timestamp(today)
            with _phase("break"):
                out = market_mod._strip_today_bar_df(df)
        self.assertEqual(len(out), 1)
        self.assertEqual(market_mod._last_bar_date(out), yesterday)

    def test_keep_when_last_is_today_after_close(self):
        today = market_mod.market_hours.now().date()
        yesterday = (pd.Timestamp(today) - pd.Timedelta(days=1)).date()
        df = _df_with_dates([yesterday.isoformat(), today.isoformat()], close=10.0)
        with mock.patch("market_hours.now") as mn:
            mn.return_value = pd.Timestamp(today)
            with _phase("closed"):
                out = market_mod._strip_today_bar_df(df)
        self.assertEqual(len(out), 2)
        self.assertEqual(market_mod._last_bar_date(out), today)

    def test_keep_when_last_is_yesterday(self):
        today = market_mod.market_hours.now().date()
        yesterday = (pd.Timestamp(today) - pd.Timedelta(days=1)).date()
        df = _df_with_dates([yesterday.isoformat()], close=10.0)
        with mock.patch("market_hours.now") as mn:
            mn.return_value = pd.Timestamp(today)
            out = market_mod._strip_today_bar_df(df)
        self.assertEqual(len(out), 1)


class TestMaybeAppendTodayBar(unittest.TestCase):
    def test_append_when_history_ends_yesterday(self):
        today = market_mod.market_hours.now().date()
        yesterday = (pd.Timestamp(today) - pd.Timedelta(days=1)).date()
        df = _df_with_dates([yesterday.isoformat()])
        quote = {
            "date": today.isoformat(),
            "open": 11.0,
            "high": 12.0,
            "low": 10.5,
            "close": 11.5,
            "volume": 5000,
        }
        with mock.patch.object(market_mod, "_daily_bar_from_quote", return_value=quote):
            with mock.patch("market_hours.is_trading_day", return_value=True):
                with _phase("trading"):
                    out = market_mod._maybe_append_today_bar("000001.SH", df)
        self.assertEqual(len(out), 2)
        self.assertEqual(out.iloc[-1]["close"], 11.5)
        self.assertEqual(out.iloc[-1]["volume"], 5000)

    def test_refresh_when_history_has_today_after_close(self):
        """收盘后源已含今日 bar, 仍以快照覆盖 (B1: 图表与盘中口径一致)。"""
        today = market_mod.market_hours.now().date()
        df = _df_with_dates([today.isoformat()], close=9.0)
        quote = {
            "date": today.isoformat(),
            "open": 9.0,
            "high": 10.0,
            "low": 8.8,
            "close": 9.6,
            "volume": 8000,
            "amount": 12345.0,
        }
        with mock.patch.object(market_mod, "_daily_bar_from_quote", return_value=quote):
            with mock.patch("market_hours.is_trading_day", return_value=True):
                with _phase("closed"):
                    out = market_mod._maybe_append_today_bar("000001.SH", df)
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[-1]["close"], 9.6)
        self.assertEqual(out.iloc[-1]["low"], 8.8)
        self.assertEqual(out.iloc[-1]["amount"], 12345.0)

    def test_no_override_when_snapshot_unavailable(self):
        """停牌/快照缺失 (volume=0 → None) 时保留源 bar, 不覆盖。"""
        today = market_mod.market_hours.now().date()
        df = _df_with_dates([today.isoformat()], close=9.0)
        with mock.patch.object(market_mod, "_daily_bar_from_quote", return_value=None):
            with mock.patch("market_hours.is_trading_day", return_value=True):
                with _phase("closed"):
                    out = market_mod._maybe_append_today_bar("000001.SH", df)
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[-1]["close"], 9.0)

    def test_no_override_on_non_trading_day(self):
        """非交易日即使源含今日 bar 也不拼快照。"""
        today = market_mod.market_hours.now().date()
        df = _df_with_dates([today.isoformat()], close=9.0)
        with mock.patch.object(market_mod, "_daily_bar_from_quote") as fq:
            with mock.patch("market_hours.is_trading_day", return_value=False):
                out = market_mod._maybe_append_today_bar("000001.SH", df)
        fq.assert_not_called()
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[-1]["close"], 9.0)

    def test_no_append_before_open(self):
        """交易日盘前: 快照是上一交易日残留, 不得拼出"今日"bar。"""
        today = market_mod.market_hours.now().date()
        yesterday = (pd.Timestamp(today) - pd.Timedelta(days=1)).date()
        df = _df_with_dates([yesterday.isoformat()])
        with mock.patch.object(market_mod, "_daily_bar_from_quote") as fq:
            with mock.patch("market_hours.is_trading_day", return_value=True):
                with _phase("pre"):
                    out = market_mod._maybe_append_today_bar("000001.SH", df)
        fq.assert_not_called()
        self.assertEqual(len(out), 1)
        self.assertEqual(market_mod._last_bar_date(out), yesterday)

    def test_refresh_last_bar_when_in_session(self):
        today = market_mod.market_hours.now().date()
        df = _df_with_dates([today.isoformat()], close=9.0)
        quote = {
            "date": today.isoformat(),
            "open": 9.0,
            "high": 10.0,
            "low": 8.8,
            "close": 9.6,
            "volume": 8000,
        }
        with mock.patch.object(market_mod, "_daily_bar_from_quote", return_value=quote):
            with mock.patch("market_hours.is_trading_day", return_value=True):
                with _phase("trading"):
                    out = market_mod._maybe_append_today_bar("510300.SH", df)
        self.assertEqual(len(out), 1)
        self.assertEqual(out.iloc[-1]["close"], 9.6)
        self.assertEqual(out.iloc[-1]["high"], 10.0)

    def test_no_append_on_non_trading_day(self):
        today = market_mod.market_hours.now().date()
        yesterday = (pd.Timestamp(today) - pd.Timedelta(days=1)).date()
        df = _df_with_dates([yesterday.isoformat()])
        with mock.patch.object(market_mod, "_daily_bar_from_quote") as fq:
            with mock.patch("market_hours.is_trading_day", return_value=False):
                out = market_mod._maybe_append_today_bar("000001.SH", df)
        fq.assert_not_called()
        self.assertEqual(len(out), 1)


class TestDailyBarFromQuoteFreshness(unittest.TestCase):
    """快照时间戳校验: 陈旧 (非今日) 快照不得拼成"今日"bar。"""

    SYM = "000001.SZ"

    def _quote(self, ts):
        return {
            "last_price": 10.5,
            "open": 10.0,
            "high": 11.0,
            "low": 9.8,
            "volume": 5000,
            "amount": 52500.0,
            "timestamp": ts,
        }

    def test_reject_stale_quote_timestamp(self):
        today = market_mod.market_hours.now().date()
        ts_yday = dt.datetime(today.year, today.month, today.day, 12, tzinfo=_CST).timestamp() - 86400
        with mock.patch("market_hours.is_trading_day", return_value=True):
            with _phase("trading"):
                with mock.patch.object(market_mod, "fetch_quotes",
                                       return_value={self.SYM: self._quote(ts_yday)}):
                    out = market_mod._daily_bar_from_quote(self.SYM, today)
        self.assertIsNone(out)

    def test_accept_today_quote_timestamp(self):
        today = market_mod.market_hours.now().date()
        ts_today = dt.datetime(today.year, today.month, today.day, 12, tzinfo=_CST).timestamp()
        with mock.patch("market_hours.is_trading_day", return_value=True):
            with _phase("trading"):
                with mock.patch.object(market_mod, "fetch_quotes",
                                       return_value={self.SYM: self._quote(ts_today)}):
                    out = market_mod._daily_bar_from_quote(self.SYM, today)
        self.assertIsNotNone(out)
        self.assertEqual(out["close"], 10.5)

    def test_no_quote_fetch_before_open(self):
        today = market_mod.market_hours.now().date()
        with mock.patch("market_hours.is_trading_day", return_value=True):
            with _phase("pre"):
                with mock.patch.object(market_mod, "fetch_quotes") as fq:
                    out = market_mod._daily_bar_from_quote(self.SYM, today)
        fq.assert_not_called()
        self.assertIsNone(out)


class TestDailyDiskCacheRoundtrip(unittest.TestCase):
    """日K 磁盘缓存读写：concat 后索引名丢失不应导致日期列无法还原。"""

    def test_cache_records_use_trade_date_column(self):
        today = market_mod.market_hours.now().date()
        dates = [
            (pd.Timestamp(today) - pd.Timedelta(days=i)).date().isoformat()
            for i in range(4, -1, -1)
        ]
        df = _df_with_dates(dates)
        df.index.name = None
        quote = {
            "date": today.isoformat(),
            "open": 11.0,
            "high": 12.0,
            "low": 10.5,
            "close": 11.5,
            "volume": 5000,
        }
        with mock.patch.object(market_mod, "_daily_bar_from_quote", return_value=quote):
            with mock.patch("market_hours.is_trading_day", return_value=True):
                with _phase("trading"):
                    merged = market_mod._maybe_append_today_bar("000975.SZ", df)
        out = merged.reset_index()
        if out.columns[0] != "trade_date":
            out = out.rename(columns={out.columns[0]: "trade_date"})
        records = out.to_dict(orient="records")
        reloaded = market_mod._normalize(pd.DataFrame(records), prefer_time=False)
        self.assertIsNotNone(reloaded)
        self.assertEqual(market_mod._last_bar_date(reloaded), today)

    def test_get_daily_bar_after_cache_hit(self):
        today = market_mod.market_hours.now().date()
        yesterday = (pd.Timestamp(today) - pd.Timedelta(days=1)).date()
        hist = _df_with_dates([yesterday.isoformat()])
        hist.index.name = "trade_date"
        quote_today = {
            "date": today.isoformat(),
            "open": 10.0,
            "high": 11.0,
            "low": 9.5,
            "close": 10.5,
            "volume": 2000,
        }
        with mock.patch.object(market_mod, "fetch_kline") as fk:
            with mock.patch.object(market_mod, "_daily_bar_from_quote", return_value=quote_today):
                with mock.patch("market_hours.is_trading_day", return_value=True):
                    with _phase("trading"):
                        merged = market_mod._maybe_append_today_bar("000975.SZ", hist.copy())
                        fk.return_value = (merged, "山金国际")
                        bar_today = market_mod.get_daily_bar("000975.SZ", today.isoformat())
                        bar_yday = market_mod.get_daily_bar("000975.SZ", yesterday.isoformat())
        self.assertIsNotNone(bar_today)
        self.assertIsNotNone(bar_yday)
        self.assertEqual(bar_yday["date"], yesterday.isoformat())


class TestDailyDiskTtl(unittest.TestCase):
    """日K 磁盘 TTL 抬高后: 当日 bar 仍每次由实时快照重拼 (契约不变)。

    冷命中要走完整数据源链 (含不可靠的 akshare 兜底), 是切换标的忽快忽慢的主因,
    故日K TTL 显著长于分钟线; 但"当日 bar 以服务端快照为准"的契约必须原样保留。
    """

    SYM = "000975.SZ"

    @staticmethod
    def _payload(dates, name="山金国际", source="alphafeed"):
        df = _df_with_dates(dates)
        df.index.name = "trade_date"
        return {"name": name, "source": source,
                "data": df.reset_index().to_dict(orient="records")}

    def test_hit_still_refreshes_today_bar_from_quote(self):
        today = market_mod.market_hours.now().date()
        dates = [
            (pd.Timestamp(today) - pd.Timedelta(days=i)).date().isoformat()
            for i in range(6, 0, -1)  # 6 根历史 (>=5, 否则 _normalize 判无效)
        ]
        quote = {
            "date": today.isoformat(), "open": 11.0, "high": 12.0,
            "low": 10.5, "close": 11.5, "volume": 5000,
        }
        timing = {}
        with mock.patch.object(market_mod._disk_cache, "get",
                               return_value=self._payload(dates)):
            with mock.patch.object(market_mod, "_daily_bar_from_quote",
                                   return_value=quote) as fq:
                with mock.patch("market_hours.is_trading_day", return_value=True):
                    with _phase("trading"):
                        df, name, source = market_mod.fetch_kline_ex(
                            self.SYM, "1d", 1006, timing=timing)
        self.assertEqual(timing["disk"], "hit")
        self.assertEqual(source, "alphafeed")
        self.assertEqual(name, "山金国际")
        # 契约: 命中磁盘缓存也要现取快照补当日 bar
        fq.assert_called_once()
        self.assertEqual(market_mod._last_bar_date(df), today)
        self.assertEqual(df.iloc[-1]["close"], 11.5)

    def test_daily_ttl_uses_configured_value(self):
        """日K TTL 走 KLINE_DISK_TTL_*, 且确实长于盘中原值 60s。"""
        seen = []

        def _get(symbol, period, count, ttl, **kw):
            seen.append(ttl)
            return None

        with mock.patch.object(market_mod._disk_cache, "get", side_effect=_get):
            with mock.patch.object(market_mod.kline_source, "fetch_kline_df",
                                   return_value=(None, None)):
                with mock.patch.object(market_mod, "market_hours") as mh:
                    mh.in_session.return_value = True
                    market_mod.fetch_kline_ex(self.SYM, "1d", 1006)
                    mh.in_session.return_value = False
                    market_mod.fetch_kline_ex(self.SYM, "1d", 1006)
        self.assertEqual(seen[0], market_mod.KLINE_DISK_TTL_SEC)
        self.assertEqual(seen[1], market_mod.KLINE_DISK_TTL_OFF_SEC)
        self.assertGreater(market_mod.KLINE_DISK_TTL_SEC, 60.0,
                           "盘中 TTL 必须长于原 60s, 否则冷命中仍是常态")
        self.assertGreater(market_mod.KLINE_DISK_TTL_OFF_SEC, 300.0,
                           "盘后 TTL 必须长于原 300s")

    def test_minute_ttl_stays_short(self):
        """分钟 bar 不拼快照, TTL 必须保持短 (否则图表滞后于实时行情)。"""
        seen = []

        def _get(symbol, period, count, ttl, **kw):
            seen.append(ttl)
            return None

        with mock.patch.object(market_mod._disk_cache, "get", side_effect=_get):
            with mock.patch.object(market_mod.kline_source, "fetch_kline_df",
                                   return_value=(None, None)):
                with mock.patch.object(market_mod, "market_hours") as mh:
                    mh.in_session.return_value = True
                    market_mod.fetch_kline_ex(self.SYM, "5m", 480)
        self.assertEqual(seen[0], 60.0)


if __name__ == "__main__":
    unittest.main()
