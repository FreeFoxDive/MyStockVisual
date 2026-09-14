# -*- coding: utf-8 -*-
"""图表当日 bar 的快照新鲜度: 复用 quote_cache (≤1.25s) 的开关与安全边界。

背景: /api/kline 的当日 bar 每次都强制 fetch_quotes(fresh=True), 实测该往返在
0~700ms 抖动, 是磁盘 TTL 抬高后热路径上仅剩的耗时来源。现允许图表路径复用
quote_cache (TTL 1.25s)。

安全边界 (本文件锁定):
1. 成交校验 (market.get_daily_bar) 与不传参的默认调用方**必须**保持 fresh=True;
2. 其余守卫一律不变: 停牌 volume=0 / 非交易日 / 盘前 / 陈旧时间戳 / 高低缺失
   都不得拼出或覆盖当日 bar;
3. 复用确实生效 (1.25s 内不再打上游), 且当日 bar 仍以快照为准 (覆盖滞后源 bar)。

运行:
    venv/Scripts/python.exe -u visual/test/test_today_bar_freshness.py
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from datetime import date
from unittest import mock

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import api.kline as kline_api
import market as market_mod

SYM = "600000.SH"
TODAY = date.today()


def _df_ending_yesterday(n=6, close=10.0):
    """n 根日K, 末根为昨天 (>=5, 否则 market._normalize 判无效)。"""
    dates = [(pd.Timestamp(TODAY) - pd.Timedelta(days=i)).date().isoformat()
             for i in range(n, 0, -1)]
    idx = pd.to_datetime(dates)
    base = pd.Series([close + i * 0.1 for i in range(n)], index=idx)
    return pd.DataFrame({
        "open": base, "high": base + 0.2, "low": base - 0.2,
        "close": base + 0.1, "volume": 1000.0, "amount": 1e6,
    }, index=idx)


def _af_quote(price=11.5, volume=5000, high=12.0, low=10.5, ts=None):
    """AlphaFeed 风格快照 (字段与 _af_quote_to_std 对齐)。"""
    return {
        "last_price": price, "prev_close": 11.0, "open": 11.0,
        "high": high, "low": low, "volume": volume,
        "amount": 1e7, "change_pct": 4.5, "amplitude": 0.1,
        "turnover_rate": 0.02, "vol_ratio": 1.2,
        "timestamp": time.time() if ts is None else ts,
        "name": "测试标的",
    }


def _phase(value):
    return mock.patch("market_hours.session_phase", return_value=value)


def _today_row(df):
    hit = df[df.index.strftime("%Y-%m-%d") == TODAY.isoformat()]
    return None if len(hit) == 0 else hit.iloc[-1]


class _Harness(unittest.TestCase):
    """统一隔离: 磁盘缓存走空(强制 miss)、不查名称表, 并记录上游行情调用次数。"""

    def setUp(self):
        market_mod.quote_cache.clear()
        self.af_calls = []
        self._quote_for = lambda s: _af_quote()

        def _fake_af(symbols):
            symbols = list(symbols)
            if symbols:
                self.af_calls.append(symbols)
            return {s: self._quote_for(s) for s in symbols}

        for p in (
            mock.patch.object(market_mod, "_fetch_af_quotes", side_effect=_fake_af),
            mock.patch.object(market_mod, "get_mr", return_value=None),
            mock.patch.object(market_mod, "_lookup_name", return_value="测试标的"),
            mock.patch.object(market_mod._disk_cache, "get", return_value=None),
            mock.patch.object(market_mod._disk_cache, "set", return_value=None),
            mock.patch.object(market_mod.kline_source, "fetch_kline_df",
                              return_value=(_df_ending_yesterday(), "alphafeed")),
            mock.patch("market_hours.is_trading_day", return_value=True),
            _phase("trading"),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _chart(self, **kw):
        """图表路径: 与 /api/kline 同参 (默认跟随端点策略常量)。"""
        kw.setdefault("quote_fresh", kline_api.TODAY_BAR_QUOTE_FRESH)
        return market_mod.fetch_kline_ex(SYM, "1d", 1006, **kw)


class TestFreshnessPolicy(_Harness):
    def test_policy_defaults_to_reuse(self):
        if os.environ.get("KLINE_TODAY_BAR_FRESH"):
            self.skipTest("本机显式设置了 KLINE_TODAY_BAR_FRESH")
        self.assertFalse(kline_api.TODAY_BAR_QUOTE_FRESH,
                         "默认应复用快照 (消除热路径 0~700ms 抖动)")

    def test_chart_path_reuses_cached_snapshot(self):
        self._chart(quote_fresh=False)
        self.assertEqual(len(self.af_calls), 1, "首次应打上游")
        self._chart(quote_fresh=False)
        self.assertEqual(len(self.af_calls), 1, "1.25s 内应复用快照缓存")

    def test_strict_mode_always_hits_upstream(self):
        """quote_fresh=True: 每次都强制拉新快照 (旧行为, 可回退)。"""
        self._chart(quote_fresh=True)
        self._chart(quote_fresh=True)
        self.assertEqual(len(self.af_calls), 2, "严格模式不得复用缓存")

    def test_reused_snapshot_produces_same_today_bar(self):
        """复用缓存得到的当日 bar 与强制新鲜的取值必须一致。"""
        stamp = time.time()
        self._quote_for = lambda s: _af_quote(price=13.3, volume=7777, ts=stamp)
        strict_df, _, _ = self._chart(quote_fresh=True)
        reused_df, _, _ = self._chart(quote_fresh=False)
        a, b = _today_row(strict_df), _today_row(reused_df)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        for col in ("open", "high", "low", "close"):
            self.assertEqual(a[col], b[col], f"当日 bar 的 {col} 不一致")
        self.assertEqual(int(a["volume"]), int(b["volume"]))

    def test_default_fetch_kline_ex_stays_strict(self):
        """不传 quote_fresh 的调用方 (tail / ETF 溢价 / 工具) 必须保持严格。"""
        self._chart(quote_fresh=True)          # 先灌热缓存
        before = len(self.af_calls)
        market_mod.fetch_kline_ex(SYM, "1d", 1006)   # 不传 quote_fresh
        self.assertEqual(len(self.af_calls), before + 1,
                         "默认必须强制新鲜, 否则会静默改变既有调用方语义")

    def test_daily_bar_from_quote_defaults_to_fresh(self):
        with mock.patch.object(market_mod, "fetch_quotes", return_value={}) as fq:
            market_mod._daily_bar_from_quote(SYM, TODAY)
        calls = [c for c in fq.call_args_list if "fresh" in c.kwargs]
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].kwargs["fresh"], "默认必须 fresh=True")

    def test_trade_validation_still_forces_fresh(self):
        """成交校验必须忽略缓存: 当日 bar 不准则买入价会被误拒 (known-issues #1)。"""
        self._chart(quote_fresh=False)          # 图表路径灌热缓存
        before = len(self.af_calls)
        with mock.patch.object(market_mod, "fetch_kline",
                               return_value=(_df_ending_yesterday(), "测试标的")):
            bar = market_mod.get_daily_bar(SYM, TODAY.isoformat())
        self.assertIsNotNone(bar)
        self.assertEqual(len(self.af_calls), before + 1,
                         "成交校验必须忽略缓存, 强制拉新快照")

    def test_endpoint_forwards_policy_constant(self):
        """/api/kline 必须按 TODAY_BAR_QUOTE_FRESH 透传 (开关才算真正接线)。"""
        from app import create_app
        app = create_app()
        with app.test_request_context(f"/api/kline?symbol={SYM}&period=1d"):
            with mock.patch.object(kline_api, "TODAY_BAR_QUOTE_FRESH", True), \
                 mock.patch.object(kline_api, "fetch_kline_ex",
                                   return_value=(_df_ending_yesterday(60),
                                                 "测试标的", "alphafeed")) as fk, \
                 mock.patch.object(kline_api, "_is_index_symbol", return_value=False), \
                 mock.patch.object(kline_api, "_is_etf", return_value=False), \
                 mock.patch.object(kline_api, "_fetch_instrument_meta",
                                   return_value={}), \
                 mock.patch.object(kline_api, "_attach_quote"):
                kline_api._kline_body(None)
        self.assertTrue(fk.call_args.kwargs.get("quote_fresh"),
                        "开关打开时端点必须传 quote_fresh=True")

    def test_endpoint_forwards_reuse_by_default(self):
        from app import create_app
        app = create_app()
        with app.test_request_context(f"/api/kline?symbol={SYM}&period=1d"):
            with mock.patch.object(kline_api, "TODAY_BAR_QUOTE_FRESH", False), \
                 mock.patch.object(kline_api, "fetch_kline_ex",
                                   return_value=(_df_ending_yesterday(60),
                                                 "测试标的", "alphafeed")) as fk, \
                 mock.patch.object(kline_api, "_is_index_symbol", return_value=False), \
                 mock.patch.object(kline_api, "_is_etf", return_value=False), \
                 mock.patch.object(kline_api, "_fetch_instrument_meta",
                                   return_value={}), \
                 mock.patch.object(kline_api, "_attach_quote"):
                kline_api._kline_body(None)
        self.assertFalse(fk.call_args.kwargs.get("quote_fresh"))


class TestGuardsHoldWithReusedSnapshot(_Harness):
    """复用快照不得削弱任何既有守卫。"""

    def test_no_append_on_non_trading_day(self):
        with mock.patch("market_hours.is_trading_day", return_value=False):
            df, _, _ = self._chart(quote_fresh=False)
        self.assertIsNone(_today_row(df), "非交易日不得拼当日 bar")

    def test_no_append_before_open(self):
        """盘前快照是上一交易日残留, 拼出来会凭空多一根「今日」bar。"""
        with _phase("pre"):
            df, _, _ = self._chart(quote_fresh=False)
        self.assertIsNone(_today_row(df), "盘前不得拼当日 bar")

    def test_suspended_symbol_volume_zero_not_applied(self):
        self._quote_for = lambda s: _af_quote(volume=0)
        df, _, _ = self._chart(quote_fresh=False)
        self.assertIsNone(_today_row(df), "停牌 (volume=0) 不得覆盖当日 bar")

    def test_stale_snapshot_timestamp_rejected(self):
        """快照时间戳非今日 (盘前/停牌残留) 时不得当今日 bar。"""
        self._quote_for = lambda s: _af_quote(ts=time.time() - 86400)
        df, _, _ = self._chart(quote_fresh=False)
        self.assertIsNone(_today_row(df), "陈旧快照不得拼成今日 bar")

    def test_missing_high_low_not_applied(self):
        self._quote_for = lambda s: _af_quote(high=None, low=None)
        df, _, _ = self._chart(quote_fresh=False)
        self.assertIsNone(_today_row(df), "高低缺失不得覆盖当日 bar")

    def test_future_bar_not_touched(self):
        """last_date > today (脏数据) 时原样返回。"""
        future = (pd.Timestamp(TODAY) + pd.Timedelta(days=1)).date().isoformat()
        df_future = pd.DataFrame(
            {"open": [10.0], "high": [10.2], "low": [9.8], "close": [10.1],
             "volume": [1000.0], "amount": [1e6]},
            index=pd.to_datetime([future]))
        with mock.patch.object(market_mod.kline_source, "fetch_kline_df",
                               return_value=(df_future, "alphafeed")):
            df, _, _ = self._chart(quote_fresh=False)
        self.assertEqual(len(df), 1)
        self.assertEqual(df.index[0].strftime("%Y-%m-%d"), future)

    def test_today_bar_still_overrides_lagging_source_bar(self):
        """核心契约: 交易日快照可用时仍要覆盖源里的滞后当日 bar。"""
        dates = [(pd.Timestamp(TODAY) - pd.Timedelta(days=i)).date().isoformat()
                 for i in range(3, -1, -1)]
        idx = pd.to_datetime(dates)
        lagging = pd.DataFrame(
            {"open": [10.0] * 4, "high": [10.5] * 4, "low": [9.5] * 4,
             "close": [10.2] * 4, "volume": [100.0] * 4, "amount": [1e6] * 4},
            index=idx)
        self._quote_for = lambda s: _af_quote(price=13.3, volume=7777,
                                              high=13.9, low=9.1)
        with mock.patch.object(market_mod.kline_source, "fetch_kline_df",
                               return_value=(lagging, "alphafeed")):
            df, _, _ = self._chart(quote_fresh=False)
        row = _today_row(df)
        self.assertIsNotNone(row)
        self.assertEqual(row["close"], 13.3, "当日 bar 必须以快照为准")
        self.assertEqual(row["low"], 9.1)
        self.assertEqual(int(row["volume"]), 7777)

    def test_consistency_between_chart_and_trade_validation(self):
        """同一快照下, 图表与成交校验得到的当日 bar 必须一致 (口径不劈叉)。"""
        dates = [(pd.Timestamp(TODAY) - pd.Timedelta(days=i)).date().isoformat()
                 for i in range(6, 0, -1)]
        idx = pd.to_datetime(dates)
        hist = pd.DataFrame(
            {"open": [10.0] * 6, "high": [10.5] * 6, "low": [9.5] * 6,
             "close": [10.2] * 6, "volume": [100.0] * 6, "amount": [1e6] * 6},
            index=idx)
        self._quote_for = lambda s: _af_quote(price=12.2, volume=4444,
                                              high=12.8, low=9.9)
        market_mod.quote_cache.clear()
        with mock.patch.object(market_mod.kline_source, "fetch_kline_df",
                               return_value=(hist, "alphafeed")):
            chart_df, _, _ = self._chart(quote_fresh=False)
            chart_row = _today_row(chart_df)
            bar = market_mod.get_daily_bar(SYM, TODAY.isoformat())
        self.assertIsNotNone(chart_row)
        self.assertIsNotNone(bar)
        self.assertEqual(chart_row["close"], bar["close"])
        self.assertEqual(chart_row["high"], bar["high"])
        self.assertEqual(chart_row["low"], bar["low"])
        self.assertEqual(int(chart_row["volume"]), int(bar["volume"]))


if __name__ == "__main__":
    unittest.main()
