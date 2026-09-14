# -*- coding: utf-8 -*-
"""基本信息 (fetch_stock_info) 数据层测试: N日涨幅/量比/交易状态/涨跌停回退/缓存。

麦蕊 get_mr 用假客户端 (SimpleNamespace), quote/日K monkeypatch, 不联网。
运行:
    venv/Scripts/python.exe -u visual/test/test_stock_info.py
"""
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import pandas as pd

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import market  # noqa: E402

TODAY = datetime(2026, 6, 30, 10, 0)


def _daily_df(closes, volumes):
    dates = pd.bdate_range(end="2026-06-30", periods=len(closes))
    return pd.DataFrame(
        {
            "open": closes, "high": closes, "low": closes, "close": closes,
            "volume": volumes, "amount": [c * v for c, v in zip(closes, volumes)],
        },
        index=dates,
    )


def _reset_caches():
    market._stock_info_cache.clear()
    market._info_quote_bases.clear()
    market._roe_cache.update({"ts": 0.0, "data": None, "ok": False})
    market._mr_instrument_cache.clear()
    market._mr_industry_cache.clear()
    market._mr_market_cache.clear()
    market._mr_company_cache.clear()


class StockInfoTest(unittest.TestCase):
    def setUp(self):
        _reset_caches()
        self.addCleanup(_reset_caches)
        self._patches = [
            mock.patch.object(market, "MAIRUI_API_KEY", "TESTKEY"),
            mock.patch.object(market, "_is_etf", return_value=False),
            mock.patch.object(market, "_is_index_symbol", return_value=False),
            mock.patch.object(market.market_hours, "is_trading_day", return_value=True),
            mock.patch.object(market.market_hours, "now", return_value=TODAY),
            mock.patch.object(market.market_hours, "session_elapsed_minutes", return_value=120.0),
            mock.patch.object(market.market_hours, "in_session", return_value=True),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _quote(self, **kw):
        q = {"name": "测试股", "volume": 1000, "amount": 1e7, "turnover_rate": 1.5,
             "prev_close": 11.85, "last_price": 12.0}
        q.update(kw)
        return q

    def _fake_mr(self, **kw):
        return types.SimpleNamespace(**kw)

    def test_cn_full_fields(self):
        # 11 根日K, 末根为今日; 前5日均量 100, 今日 1000 → 量比 20
        closes = [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 20]
        volumes = [100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 1000]
        df = _daily_df(closes, volumes)
        mr = self._fake_mr(
            hsdc_himk_roe=lambda: [{"dm": "000001", "mc": "平安银行", "hym": "银行Ⅱ",
                                    "syld": 4.43, "sjl": 0.49}],
            concepts_of_stock=lambda code: [{"code": "sw_yx", "name": "A股-申万行业-银行"}],
            stock_instrument=lambda s: {"up": 13.04, "dp": 10.67, "pc": 11.85, "fv": 1, "tv": 2},
        )
        with mock.patch.object(market, "get_mr", return_value=mr), \
             mock.patch.object(market, "fetch_quote", return_value=self._quote()), \
             mock.patch.object(market, "fetch_kline_ex", return_value=(df, "平安银行", "mairui")):
            info = market.fetch_stock_info("000001.SZ")

        self.assertEqual(info["industry"], "银行", "行业取申万条目 (覆盖 hym)")
        self.assertEqual(info["pe"], 4.43)
        self.assertEqual(info["pb"], 0.49)
        self.assertEqual(info["limit_up"], 13.04)
        self.assertEqual(info["limit_down"], 10.67)
        self.assertFalse(info["limit_estimated"])
        self.assertAlmostEqual(info["vol_ratio"], 20.0, places=4)
        # N 日涨幅 = 末收 vs N 根之前 (closes[-1-n])
        self.assertAlmostEqual(info["chg_3d"], (20 - 16) / 16 * 100, places=4)
        self.assertAlmostEqual(info["chg_5d"], (20 - 14) / 14 * 100, places=4)
        self.assertAlmostEqual(info["chg_10d"], (20 - 9) / 9 * 100, places=4)
        self.assertEqual(info["volume"], 1000)
        self.assertEqual(info["amount"], 1e7)
        self.assertEqual(info["turnover_rate"], 1.5)
        self.assertEqual((info["trade_status"], info["trade_status_text"]), ("trading", "交易中"))

    def test_quote_updates_derived_fields_without_refetching_history(self):
        market._info_quote_bases["000001.SZ"] = {
            "day": TODAY.date(), "closes": {3: 10, 5: 8, 10: 5}, "avg_volume": 100,
        }
        quote = {"last_price": 12, "volume": 1000}
        with mock.patch.object(market.market_hours, "session_elapsed_minutes", return_value=120):
            live = market._live_info_for_quote("000001.SZ", quote)
        self.assertAlmostEqual(live["vol_ratio"], 20)
        self.assertAlmostEqual(live["chg_3d"], 20)
        self.assertAlmostEqual(live["chg_5d"], 50)
        self.assertAlmostEqual(live["chg_10d"], 140)
        self.assertEqual(quote, {"last_price": 12, "volume": 1000})

    def test_volume_ratio_last_session_when_not_today(self):
        # 收盘后/休市: 末根非今日 → 最近一个交易日全天口径 = 当日量 / 前5日均量
        closes = [10] * 8
        df = _daily_df(closes, [100] * 8)
        df.index = pd.to_datetime(["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04",
                                   "2026-06-05", "2026-06-08", "2026-06-09", "2026-06-10"])
        self.assertAlmostEqual(market._volume_ratio_from_df(df), 1.0, places=6)

        # 末根放量 300 → 全天量比 3.0
        df2 = _daily_df(closes, [100, 100, 100, 100, 100, 100, 100, 300])
        df2.index = df.index
        self.assertAlmostEqual(market._volume_ratio_from_df(df2), 3.0, places=6)

    def test_volume_ratio_none_pre_open_or_no_volume(self):
        # 今日已有 bar 但未开盘 (已过分钟=0) → 盘中量比无意义
        df = _daily_df([10] * 8, [100] * 7 + [500])  # 末根为 TODAY
        with mock.patch.object(market.market_hours, "session_elapsed_minutes", return_value=0.0):
            self.assertIsNone(market._volume_ratio_from_df(df))
        # 今日 bar 无成交 (volume=0) → None
        df0 = _daily_df([10] * 8, [100] * 7 + [0])
        self.assertIsNone(market._volume_ratio_from_df(df0))

    def test_limit_fallback_estimated(self):
        df = _daily_df([10] * 8, [100] * 8)
        mr = self._fake_mr(
            hsdc_himk_roe=lambda: [],
            stock_instrument=mock.Mock(side_effect=RuntimeError("boom")),
        )
        with mock.patch.object(market, "get_mr", return_value=mr), \
             mock.patch.object(market, "fetch_quote", return_value=self._quote(prev_close=10.0)), \
             mock.patch.object(market, "fetch_kline_ex", return_value=(df, "x", "mairui")):
            info = market.fetch_stock_info("600000.SH")
        self.assertEqual(info["limit_up"], 11.0)
        self.assertEqual(info["limit_down"], 9.0)
        self.assertTrue(info["limit_estimated"])
        self.assertIsNone(info["pe"])
        self.assertIsNone(info["industry"])

    def test_non_cn_skips_mairui(self):
        df = _daily_df([10] * 8, [100] * 8)
        mr = mock.Mock()
        with mock.patch.object(market, "get_mr", return_value=mr), \
             mock.patch.object(market, "fetch_quote", return_value=self._quote(prev_close=20.0)), \
             mock.patch.object(market, "fetch_kline_ex", return_value=(df, "x", "alphafeed")):
            info = market.fetch_stock_info("AAPL")
        self.assertIsNone(info["industry"])
        self.assertIsNone(info["pe"])
        self.assertIsNone(info["pb"])
        self.assertIsNone(info["limit_up"], "港股/美股无涨跌停, 甚至不回退")
        self.assertEqual(info["trade_status_text"], "—")
        mr.hsdc_himk_roe.assert_not_called()
        mr.stock_instrument.assert_not_called()

    def test_cache_avoids_refetch(self):
        df = _daily_df([10] * 8, [100] * 8)
        mr = self._fake_mr(hsdc_himk_roe=lambda: [])
        fetch = mock.Mock(return_value=self._quote())
        with mock.patch.object(market, "get_mr", return_value=mr), \
             mock.patch.object(market, "fetch_quote", fetch), \
             mock.patch.object(market, "fetch_kline_ex", return_value=(df, "x", "mairui")):
            market.fetch_stock_info("000001.SZ")
            n1 = fetch.call_count
            market.fetch_stock_info("000001.SZ")
            self.assertEqual(fetch.call_count, n1, "60s 内应命中缓存")
            market.fetch_stock_info("000001.SZ", force=True)
            self.assertGreater(fetch.call_count, n1, "force=True 应重取")

    def test_trade_status_branches(self):
        now = market.market_hours.now
        with mock.patch.object(market.market_hours, "is_trading_day", return_value=False):
            self.assertEqual(market._trade_status(None), ("closed", "休市"))
        with mock.patch.object(market.market_hours, "now", return_value=datetime(2026, 6, 30, 9, 0)), \
             mock.patch.object(market.market_hours, "session_elapsed_minutes", return_value=0.0):
            self.assertEqual(market._trade_status(self._quote()), ("pre", "未开盘"))
        with mock.patch.object(market.market_hours, "now", return_value=datetime(2026, 6, 30, 11, 45)), \
             mock.patch.object(market.market_hours, "in_session", return_value=False):
            self.assertEqual(market._trade_status(self._quote()), ("break", "午间休市"))
        with mock.patch.object(market.market_hours, "now", return_value=datetime(2026, 6, 30, 15, 30)), \
             mock.patch.object(market.market_hours, "in_session", return_value=False), \
             mock.patch.object(market.market_hours, "session_elapsed_minutes", return_value=240.0):
            self.assertEqual(market._trade_status(self._quote()), ("closed", "已收盘"))
        self.assertEqual(market._trade_status(self._quote(volume=0))[0], "halt")
        self.assertEqual(now(), TODAY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
