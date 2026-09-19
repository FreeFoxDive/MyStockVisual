"""fetch_quotes AF 优先 + 麦蕊回退单测。"""
from __future__ import annotations

import os
import sys
import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest import mock

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import market as market_mod
import perf


def _af_row(symbol, **kw):
    base = {
        "symbol": symbol,
        "last_price": 10.5,
        "prev_close": 10.0,
        "open": 10.1,
        "high": 10.6,
        "low": 10.0,
        "volume": 1000,
        "amount": 10500.0,
        "name": symbol,
    }
    base.update(kw)
    return base


def _mr_row(**kw):
    base = {"p": 10.5, "yc": 10.0, "o": 10.1, "h": 10.6, "l": 10.0, "v": 1000, "cje": 10500.0}
    base.update(kw)
    return base


def _is_index(sym):
    return sym.endswith(".SH") and sym.startswith("000")


def _is_etf(sym):
    return sym.startswith("51") or sym.startswith("15")


@contextmanager
def _depth_iso(fresh=True):
    """五档用例的隔离环境 (test_fetch_depth_* 共用)。`fresh=True` 连带清 TTL 五档缓存
    (首个调用点用); 需要验证「二次调用命中 TTL 缓存」的用例传 `fresh=False`。

    两件事, 都为了不依赖「跑测试时刚好是盘中」这个隐含前提:
      * 当日快照文件换成「读不到」的 Mock —— 既不读磁盘上当天遗留的快照, 也不把 mock 出来的
        fixture 写进真实 .cache/depth_day.json。盘后/周末跑时 `_fetch_depth_locked` 会命中
        `_depth_day_get` 短路, 直接返回磁盘上那份快照, 用例根本走不到自己 mock 的上游
        (曾在全天盘后把这三项跑成红色);
      * `is_live` 显式为真 —— 这些用例测的是上游五档的字段映射与回退
        (取数门控已统一到 is_live: 含集合竞价, 不含午休)。
    """
    if fresh:
        market_mod._depth_cache._cache.clear()
    market_mod._depth_day_cache.clear()
    market_mod._depth_day_cache_date = None
    market_mod._depth_bucket = None
    fake_file = mock.Mock()
    fake_file.read_text.side_effect = FileNotFoundError()
    with mock.patch.object(market_mod, "_DEPTH_DAY_CACHE_FILE", fake_file), \
         mock.patch.object(market_mod.market_hours, "is_live", return_value=True):
        yield fake_file


class TestFetchQuotesAfFirst(unittest.TestCase):
    def setUp(self):
        market_mod.quote_cache._cache.clear()
        market_mod.quote_snapshot_cache._cache.clear()
        self.p_index = mock.patch.object(market_mod, "_is_index_symbol", side_effect=_is_index)
        self.p_etf = mock.patch.object(market_mod, "_is_etf", side_effect=_is_etf)
        self.p_index.start()
        self.p_etf.start()
        # 麦蕊回退预算: 默认换成大额度新桶, 避免用例间互相占用
        self.p_mr_budget = mock.patch.object(
            market_mod, "_mr_quote_budget", market_mod.PacedBudget(1000))
        self.p_mr_budget.start()

    def tearDown(self):
        self.p_index.stop()
        self.p_etf.stop()
        self.p_mr_budget.stop()

    def test_index_uses_af_not_mairui(self):
        api = mock.Mock()
        with mock.patch.object(market_mod, "_fetch_af_quotes", return_value={"000001.SH": _af_row("000001.SH")}):
            with mock.patch.object(market_mod, "get_mr", return_value=api):
                out = market_mod.fetch_quotes(["000001.SH"], fresh=True)
        self.assertEqual(out["000001.SH"]["last_price"], 10.5)
        api.index_real_time.assert_not_called()

    def test_etf_uses_af_not_mairui(self):
        api = mock.Mock()
        with mock.patch.object(market_mod, "_fetch_af_quotes", return_value={"510300.SH": _af_row("510300.SH")}):
            with mock.patch.object(market_mod, "get_mr", return_value=api):
                out = market_mod.fetch_quotes(["510300.SH"], fresh=True)
        self.assertEqual(out["510300.SH"]["high"], 10.6)
        api.fund_real_time.assert_not_called()

    def test_stock_uses_af_not_mairui(self):
        api = mock.Mock()
        with mock.patch.object(market_mod, "_fetch_af_quotes", return_value={"600519.SH": _af_row("600519.SH")}):
            with mock.patch.object(market_mod, "get_mr", return_value=api):
                out = market_mod.fetch_quotes(["600519.SH"], fresh=True)
        self.assertEqual(out["600519.SH"]["volume"], 1000)
        api.stock_ssjy_more.assert_not_called()

    def test_index_fallback_mairui_when_af_empty(self):
        api = mock.Mock()
        api.index_real_time.return_value = _mr_row()
        with mock.patch.object(market_mod, "_fetch_af_quotes", return_value={}):
            with mock.patch.object(market_mod, "get_mr", return_value=api):
                out = market_mod.fetch_quotes(["000001.SH"], fresh=True)
        api.index_real_time.assert_called_once_with("000001.SH")
        self.assertEqual(out["000001.SH"]["last_price"], 10.5)

    def test_etf_fallback_mairui_when_af_empty(self):
        api = mock.Mock()
        api.fund_real_time.return_value = _mr_row(hs=0.42, lb=1.35)
        with mock.patch.object(market_mod, "_fetch_af_quotes", return_value={}):
            with mock.patch.object(market_mod, "get_mr", return_value=api):
                out = market_mod.fetch_quotes(["510300.SH"], fresh=True)
        api.fund_real_time.assert_called_once_with("510300")
        self.assertEqual(out["510300.SH"]["last_price"], 10.5)
        self.assertEqual(out["510300.SH"]["symbol"], "510300.SH")
        self.assertAlmostEqual(out["510300.SH"]["turnover_rate"], 0.42)
        self.assertAlmostEqual(out["510300.SH"]["vol_ratio"], 1.35)

    def test_stock_fallback_mairui_when_af_empty(self):
        api = mock.Mock()
        api.stock_ssjy_more.return_value = [{"dm": "600519", **_mr_row()}]
        with mock.patch.object(market_mod, "_fetch_af_quotes", return_value={}):
            with mock.patch.object(market_mod, "get_mr", return_value=api):
                out = market_mod.fetch_quotes(["600519.SH"], fresh=True)
        api.stock_ssjy_more.assert_called_once()
        self.assertEqual(out["600519.SH"]["last_price"], 10.5)

    def test_mairui_fallback_budget_exhausted_skips(self):
        """麦蕊回退预算耗尽时不再打麦蕊, 无缓存则该标的缺失 (保护免费额度)。"""
        api = mock.Mock()
        api.index_real_time.return_value = _mr_row()
        with mock.patch.object(market_mod, "_fetch_af_quotes", return_value={}):
            with mock.patch.object(market_mod, "get_mr", return_value=api):
                with mock.patch.object(market_mod, "_mr_quote_budget",
                                       market_mod.PacedBudget(1)):
                    market_mod._mr_quote_budget.try_acquire()  # 预支唯一令牌
                    out = market_mod.fetch_quotes(["000001.SH"], fresh=True)
        api.index_real_time.assert_not_called()
        self.assertNotIn("000001.SH", out)

    def test_mairui_fallback_budget_bounds_calls(self):
        """预算 1/min: 多标的回退只发 1 次麦蕊调用, 其余跳过。"""
        api = mock.Mock()
        api.index_real_time.return_value = _mr_row()
        with mock.patch.object(market_mod, "_fetch_af_quotes", return_value={}):
            with mock.patch.object(market_mod, "get_mr", return_value=api):
                with mock.patch.object(market_mod, "_mr_quote_budget",
                                       market_mod.PacedBudget(1)):
                    out = market_mod.fetch_quotes(
                        ["000001.SH", "000012.SH"], fresh=True)
        api.index_real_time.assert_called_once_with("000001.SH")
        self.assertIn("000001.SH", out)
        self.assertNotIn("000012.SH", out)

    def test_af_missing_high_low_falls_back(self):
        api = mock.Mock()
        api.index_real_time.return_value = _mr_row()
        with mock.patch.object(market_mod, "_fetch_af_quotes", return_value={}):
            with mock.patch.object(market_mod, "get_mr", return_value=api):
                out = market_mod.fetch_quotes(["000001.SH"], fresh=True)
        api.index_real_time.assert_called_once()
        self.assertEqual(out["000001.SH"]["last_price"], 10.5)

    def test_af_quote_valid_rejects_missing_high(self):
        self.assertFalse(market_mod._af_quote_valid(_af_row("000001.SH", high=None)))
        self.assertTrue(market_mod._af_quote_valid(_af_row("000001.SH")))

    def test_mixed_batch_partial_af_partial_mairui(self):
        api = mock.Mock()
        api.fund_real_time.return_value = _mr_row(p=20.0)
        with mock.patch.object(
            market_mod,
            "_fetch_af_quotes",
            return_value={"000001.SH": _af_row("000001.SH")},
        ):
            with mock.patch.object(market_mod, "get_mr", return_value=api):
                out = market_mod.fetch_quotes(["000001.SH", "510300.SH"], fresh=True)
        self.assertEqual(out["000001.SH"]["last_price"], 10.5)
        api.index_real_time.assert_not_called()
        api.fund_real_time.assert_called_once_with("510300")
        self.assertEqual(out["510300.SH"]["last_price"], 20.0)

    def test_af_quote_to_std_turnover_decimal_to_pct(self):
        raw = _af_row("600519.SH", turnover_rate=0.015, amplitude=0.02)
        with mock.patch.object(market_mod, "_lookup_name", return_value="x"):
            out = market_mod._af_quote_to_std(raw, "600519.SH")
        self.assertAlmostEqual(out["turnover_rate"], 1.5)
        self.assertAlmostEqual(out["amplitude"], 2.0)

    def test_af_quote_to_std_turnover_none(self):
        raw = _af_row("600519.SH")
        with mock.patch.object(market_mod, "_lookup_name", return_value="x"):
            out = market_mod._af_quote_to_std(raw, "600519.SH")
        self.assertIsNone(out["turnover_rate"])
        self.assertIsNone(out["amplitude"])

    def test_af_quote_to_std_maps_fields(self):
        # _af_row 模拟 _row_to_quote 输出 (change_pct 已是百分数)
        raw = _af_row("600519.SH", name="茅台", change_pct=5.0)
        with mock.patch.object(market_mod, "_lookup_name", return_value="备用名"):
            out = market_mod._af_quote_to_std(raw, "600519.SH")
        self.assertEqual(out["last_price"], 10.5)
        self.assertEqual(out["name"], "茅台")
        self.assertEqual(out["change_pct"], 5.0)
        raw2 = _af_row("600519.SH", name=None)
        with mock.patch.object(market_mod, "_lookup_name", return_value="备用名"):
            out2 = market_mod._af_quote_to_std(raw2, "600519.SH")
        self.assertEqual(out2["name"], "备用名")

    def test_row_to_quote_change_pct_decimal_to_pct(self):
        """单位契约: AF ext.change_pct 官方是小数 (0.01=1%), 唯一出口 _row_to_quote
        统一转百分数, 保证 monitor 涨跌幅预警与前端拿到同一口径。"""
        from feed import _row_to_quote
        row = {
            "symbol": "600519.SH", "last_price": 10.5, "prev_close": 10.0,
            "open": 10.1, "high": 10.6, "low": 10.0, "volume": 1000,
            "amount": 10500.0, "timestamp": 1_700_000_000_000,
            "ext.name": "茅台", "ext.change_pct": 0.05,
            "ext.turnover_rate": 0.015, "ext.amplitude": 0.02,
        }
        q = _row_to_quote(row)
        self.assertAlmostEqual(q["change_pct"], 5.0)
        # None 保持 None, 不产生 0 值假涨跌
        row2 = dict(row, **{"ext.change_pct": None})
        self.assertIsNone(_row_to_quote(row2)["change_pct"])

    def test_af_chain_percent_end_to_end(self):
        """AF 原始行 → _row_to_quote → _af_quote_to_std 全线百分数。"""
        import pandas as pd
        from feed import _row_to_quote
        df = pd.DataFrame([{
            "symbol": "600519.SH", "last_price": 10.5, "prev_close": 10.0,
            "open": 10.1, "high": 10.6, "low": 10.0, "volume": 1000,
            "amount": 10500.0, "timestamp": 1_700_000_000_000,
            "ext.change_pct": -0.0234, "ext.turnover_rate": 0.015,
            "ext.amplitude": 0.02,
        }])
        q = market_mod._af_quote_to_std(_row_to_quote(df.iloc[0]), "600519.SH")
        self.assertAlmostEqual(q["change_pct"], -2.34)
        self.assertAlmostEqual(q["turnover_rate"], 1.5)
        self.assertAlmostEqual(q["amplitude"], 2.0)

    def test_af_quote_to_std_carries_epoch_timestamp(self):
        raw = _af_row("600519.SH", timestamp=1789056000)
        with mock.patch.object(market_mod, "_lookup_name", return_value="x"):
            out = market_mod._af_quote_to_std(raw, "600519.SH")
        self.assertEqual(out["timestamp"], 1789056000.0)
        # 毫秒自动降级为秒
        raw_ms = _af_row("600519.SH", timestamp=1789056000000)
        with mock.patch.object(market_mod, "_lookup_name", return_value="x"):
            out_ms = market_mod._af_quote_to_std(raw_ms, "600519.SH")
        self.assertEqual(out_ms["timestamp"], 1789056000.0)

    def test_mr_quote_to_std_timestamp_none_when_unparseable(self):
        raw = _mr_row(t="2026-09-11 15:00:00")
        out = market_mod._mr_quote_to_std(raw, "000001.SZ")
        self.assertIsNone(out["timestamp"])

    def test_safe_epoch(self):
        self.assertEqual(market_mod._safe_epoch(1789056000), 1789056000.0)
        self.assertEqual(market_mod._safe_epoch(1789056000000), 1789056000.0)
        self.assertIsNone(market_mod._safe_epoch(None))
        self.assertIsNone(market_mod._safe_epoch("2026-09-11"))
        self.assertIsNone(market_mod._safe_epoch(0))
        # 14 位紧凑日期串 (YYYYMMDDHHMMSS) 不得被当毫秒/epoch
        self.assertIsNone(market_mod._safe_epoch("20260911150000"))
        self.assertIsNone(market_mod._safe_epoch(20260911150000))

    def test_fetch_af_quotes_batch_and_valid(self):
        import pandas as pd
        af = mock.Mock()
        af.quotes.get.return_value = pd.DataFrame([
            {"symbol": "600519.SH", "last_price": 10.0, "high": 10.5, "low": 9.5, "volume": 100},
            {"symbol": "000001.SH", "last_price": 10.0, "high": None, "low": 9.5, "volume": 100},
        ])
        with mock.patch.object(market_mod, "AF_API_KEY", "test-key"):
            with mock.patch.object(market_mod, "get_af", return_value=af):
                out = market_mod._fetch_af_quotes(["600519.SH", "000001.SH"])
        self.assertIn("600519.SH", out)
        self.assertNotIn("000001.SH", out)

    def test_fetch_depth_maps_fields(self):
        af = mock.Mock()
        af.depth.get.return_value = {
            "symbol": "600519.SH", "timestamp": 1,
            "bid_prices": [10.0, 9.9], "bid_volumes": [1, 2],
            "ask_prices": [10.1, 10.2], "ask_volumes": [3, 4],
        }
        with _depth_iso(), \
             mock.patch.object(market_mod, "AF_API_KEY", "k"), \
             mock.patch.object(market_mod, "get_af", return_value=af):
            out = market_mod.fetch_depth("600519.SH")
        self.assertEqual(out["bid_prices"], [10.0, 9.9])
        self.assertEqual(out["ask_volumes"], [3, 4])
        # 二次调用走缓存 (不再打 API): 不清 TTL 缓存, 验证真的命中
        with _depth_iso(fresh=False), \
             mock.patch.object(market_mod, "AF_API_KEY", "k"), \
             mock.patch.object(market_mod, "get_af", return_value=af):
            out2 = market_mod.fetch_depth("600519.SH")
        self.assertIs(out, out2)
        af.depth.get.assert_called_once()

    def test_fetch_depth_none_on_empty(self):
        af = mock.Mock()
        af.depth.get.return_value = {}
        with _depth_iso(), \
             mock.patch.object(market_mod, "AF_API_KEY", "k"), \
             mock.patch.object(market_mod, "get_af", return_value=af):
            self.assertIsNone(market_mod.fetch_depth("600519.SH"))

    def test_fetch_depth_zero_padded_levels_become_empty(self):
        """上游缺档位时补 0.0, 不是少给几项 —— 0 价必须变空档, 不能画成报价。

        payload 用 588200.SH 2026-09-18 14:59:14 的实测形状 (收盘集合竞价)。
        """
        af = mock.Mock()
        af.depth.get.return_value = {
            "symbol": "588200.SH", "timestamp": 1789714754000,
            "bid_prices": [1.184, 0.0, 0.0, 0.0, 0.0],
            "bid_volumes": [174867, 656, 0, 0, 0],
            "ask_prices": [1.184, 0.0, 0.0, 0.0, 0.0],
            "ask_volumes": [174867, 0, 0, 0, 0],
        }
        with _depth_iso(), \
             mock.patch.object(market_mod, "AF_API_KEY", "k"), \
             mock.patch.object(market_mod, "get_af", return_value=af):
            out = market_mod.fetch_depth("588200.SH")
        self.assertEqual(out["bid_prices"], [1.184, None, None, None, None])
        self.assertEqual(out["ask_prices"], [1.184, None, None, None, None])
        # 买2 那 656 手挂在 0 价上, 是无主数据, 跟价一起置空
        self.assertEqual(out["bid_volumes"], [174867, None, None, None, None])

    def test_fetch_depth_all_zero_book_is_no_data(self):
        """全 0 的盘口 (停牌/未开盘上游占位) 不是"五档是空的", 要回退而不是下发。"""
        af = mock.Mock()
        af.depth.get.return_value = {
            "symbol": "600519.SH", "timestamp": 1,
            "bid_prices": [0.0] * 5, "bid_volumes": [0] * 5,
            "ask_prices": [0.0] * 5, "ask_volumes": [0] * 5,
        }
        with _depth_iso(), \
             mock.patch.object(market_mod, "AF_API_KEY", "k"), \
             mock.patch.object(market_mod, "MAIRUI_API_KEY", ""), \
             mock.patch.object(market_mod, "get_af", return_value=af):
            self.assertIsNone(market_mod.fetch_depth("600519.SH"))

    def test_fetch_depth_falls_back_to_mairui_five(self):
        market_mod._mr_depth_bucket = market_mod.PacedBudget(24)
        mr = mock.Mock()
        mr.stock_real_five.return_value = [{
            "t": "2026-09-14 10:00:00",
            "pb1": 10.0, "vb1": 100, "pb2": 9.9, "vb2": 90,
            "ps1": 10.1, "vs1": 120, "ps2": 10.2, "vs2": 130,
        }]
        with _depth_iso(), \
             mock.patch.object(market_mod, "AF_API_KEY", ""), \
             mock.patch.object(market_mod, "MAIRUI_API_KEY", "k"), \
             mock.patch.object(market_mod, "get_mr", return_value=mr):
            out = market_mod.fetch_depth("600519.SH")
        self.assertEqual(out["bid_prices"][:2], [10.0, 9.9])
        self.assertEqual(out["ask_volumes"][:2], [120, 130])
        mr.stock_real_five.assert_called_once_with("600519")

    def test_quote_cache_skips_af(self):
        api = mock.Mock()
        with mock.patch.object(market_mod, "_fetch_af_quotes") as fetch_af:
            fetch_af.return_value = {"600519.SH": _af_row("600519.SH")}
            with mock.patch.object(market_mod, "get_mr", return_value=api):
                first = market_mod.fetch_quotes(["600519.SH"], fresh=True)
                second = market_mod.fetch_quotes(["600519.SH"], fresh=False)
        self.assertEqual(first["600519.SH"]["last_price"], 10.5)
        self.assertEqual(second["600519.SH"]["last_price"], 10.5)
        fetch_af.assert_called_once()


def _std_quote(symbol, last=9.9):
    return {"symbol": symbol, "last_price": last, "prev_close": 10.0,
            "change_pct": 1.0, "timestamp": 1, "_revision": 1}


class OffSessionSnapshotTest(unittest.TestCase):
    """非活跃时段的长 TTL 最后快照: 页面跳转不再因预算/锁抖动返回 {}。

    门控口径是 is_live: 集合竞价必须走实时路径, 否则这份 12h 快照会命中并
    顶掉竞价撮合价 —— 所以这里 patch 的是 is_live 而不是 in_session。
    """

    def setUp(self):
        market_mod.quote_cache._cache.clear()
        market_mod.quote_snapshot_cache._cache.clear()
        self.p_index = mock.patch.object(market_mod, "_is_index_symbol", side_effect=_is_index)
        self.p_etf = mock.patch.object(market_mod, "_is_etf", side_effect=_is_etf)
        self.p_mr_budget = mock.patch.object(
            market_mod, "_mr_quote_budget", market_mod.PacedBudget(1000))
        self.p_index.start()
        self.p_etf.start()
        self.p_mr_budget.start()
        self.addCleanup(self.p_index.stop)
        self.addCleanup(self.p_etf.stop)
        self.addCleanup(self.p_mr_budget.stop)
        self.addCleanup(market_mod.quote_snapshot_cache._cache.clear)

    def test_off_session_serves_snapshot_without_upstream(self):
        market_mod.quote_snapshot_cache.set("600519.SH", _std_quote("600519.SH", 9.9))
        with mock.patch.object(market_mod.market_hours, "is_live", return_value=False), \
             mock.patch.object(market_mod, "_fetch_af_quotes") as fetch_af, \
             mock.patch.object(market_mod, "get_mr") as get_mr:
            out = market_mod.fetch_quotes(["600519.SH"])
        self.assertEqual(out["600519.SH"]["last_price"], 9.9)
        fetch_af.assert_not_called()
        get_mr.assert_not_called()

    def test_off_session_lock_contended_falls_back_to_snapshot(self):
        market_mod.quote_snapshot_cache.set("600519.SH", _std_quote("600519.SH", 9.9))
        acquired = market_mod._quote_fetch_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            with mock.patch.object(market_mod.market_hours, "is_live", return_value=False):
                out = market_mod.fetch_quotes(["600519.SH"])
        finally:
            market_mod._quote_fetch_lock.release()
        self.assertEqual(out["600519.SH"]["last_price"], 9.9)

    def test_off_session_budget_denied_returns_snapshot(self):
        market_mod.quote_snapshot_cache.set("600519.SH", _std_quote("600519.SH", 9.9))
        with mock.patch.object(market_mod.market_hours, "is_live", return_value=False), \
             mock.patch.object(market_mod, "_fetch_af_quotes", return_value={}), \
             mock.patch.object(market_mod, "get_mr", return_value=mock.Mock()):
            out = market_mod.fetch_quotes(["600519.SH"], fresh=True)
        self.assertEqual(out["600519.SH"]["last_price"], 9.9)

    def test_snapshot_written_on_emit_and_survives_cache_expiry(self):
        with mock.patch.object(market_mod, "_fetch_af_quotes",
                               return_value={"600519.SH": _af_row("600519.SH")}), \
             mock.patch.object(market_mod, "get_mr", return_value=mock.Mock()):
            market_mod.fetch_quotes(["600519.SH"], fresh=True)
        self.assertIsNotNone(market_mod.quote_snapshot_cache.get("600519.SH"))
        market_mod.quote_cache._cache.clear()  # 模拟 1.25s 实时缓存过期
        with mock.patch.object(market_mod.market_hours, "is_live", return_value=False):
            out = market_mod.fetch_quotes(["600519.SH"])
        self.assertEqual(out["600519.SH"]["last_price"], 10.5)

    def test_live_ignores_snapshot(self):
        market_mod.quote_snapshot_cache.set("600519.SH", _std_quote("600519.SH", 99.0))
        with mock.patch.object(market_mod.market_hours, "is_live", return_value=True), \
             mock.patch.object(market_mod, "_fetch_af_quotes",
                               return_value={"600519.SH": _af_row("600519.SH")}), \
             mock.patch.object(market_mod, "get_mr", return_value=mock.Mock()) as get_mr:
            out = market_mod.fetch_quotes(["600519.SH"])
        self.assertEqual(out["600519.SH"]["last_price"], 10.5)
        self.assertNotEqual(out["600519.SH"]["last_price"], 99.0)

    def test_live_no_upstream_no_snapshot_fallback(self):
        market_mod.quote_snapshot_cache.set("600519.SH", _std_quote("600519.SH", 99.0))
        with mock.patch.object(market_mod.market_hours, "is_live", return_value=True), \
             mock.patch.object(market_mod, "_fetch_af_quotes", return_value={}), \
             mock.patch.object(market_mod, "get_mr", return_value=mock.Mock()):
            out = market_mod.fetch_quotes(["600519.SH"], fresh=True)
        self.assertEqual(out, {})


class DepthDayCacheTest(unittest.TestCase):
    def setUp(self):
        market_mod._depth_cache._cache.clear()
        market_mod._depth_day_cache.clear()
        market_mod._depth_day_cache_date = None
        market_mod._depth_bucket = None
        fake_file = mock.Mock()
        fake_file.read_text.side_effect = FileNotFoundError()
        self.file_patch = mock.patch.object(
            market_mod, "_DEPTH_DAY_CACHE_FILE", fake_file
        )
        self.file_patch.start()
        self.addCleanup(self.file_patch.stop)

    def _af(self):
        af = mock.Mock()
        af.depth.get.return_value = {
            "symbol": "002472.SZ", "timestamp": 1,
            "bid_prices": [10], "bid_volumes": [100],
            "ask_prices": [11], "ask_volumes": [200],
        }
        return af

    def test_closed_and_weekend_keep_last_trading_day_depth(self):
        af = self._af()
        friday = datetime(2026, 9, 11, 14, 30)
        with mock.patch.object(market_mod, "AF_API_KEY", "k"), \
             mock.patch.object(market_mod, "get_af", return_value=af), \
             mock.patch.object(market_mod.market_hours, "now", return_value=friday), \
             mock.patch.object(market_mod.market_hours, "is_trading_day", return_value=True), \
             mock.patch.object(market_mod.market_hours, "is_live", return_value=True):
            first = market_mod.fetch_depth("002472.SZ")
        market_mod._depth_cache._cache.clear()
        saturday = datetime(2026, 9, 12, 12, 0)
        with mock.patch.object(market_mod.market_hours, "now", return_value=saturday), \
             mock.patch.object(market_mod.market_hours, "is_trading_day", return_value=False), \
             mock.patch.object(market_mod.market_hours, "is_live", return_value=False):
            self.assertEqual(market_mod.fetch_depth("002472.SZ"), first)
        af.depth.get.assert_called_once()

    def test_closing_auction_snapshot_does_not_replace_replayable_book(self):
        """14:57-15:00 的塌陷盘口不覆盖当天最后一份完整五档 (588200.SH 的坏法)。

        实时看的是当时的真相 (竞价队列), 但收盘后回放必须还是 14:56 那份。
        """
        af = mock.Mock()
        af.depth.get.side_effect = [
            {"symbol": "588200.SH", "timestamp": 1789714574000,
             "bid_prices": [1.183, 1.182, 1.181, 1.18, 1.179],
             "bid_volumes": [30354, 30831, 27963, 39576, 15446],
             "ask_prices": [1.184, 1.185, 1.186, 1.187, 1.188],
             "ask_volumes": [21647, 59151, 33397, 22978, 30883]},
            {"symbol": "588200.SH", "timestamp": 1789714754000,
             "bid_prices": [1.184, 0.0, 0.0, 0.0, 0.0],
             "bid_volumes": [174867, 656, 0, 0, 0],
             "ask_prices": [1.184, 0.0, 0.0, 0.0, 0.0],
             "ask_volumes": [174867, 0, 0, 0, 0]},
        ]
        with mock.patch.object(market_mod, "AF_API_KEY", "k"), \
             mock.patch.object(market_mod, "get_af", return_value=af), \
             mock.patch.object(market_mod.market_hours, "is_trading_day", return_value=True):
            # 五档限频桶 24/min = 每 2.5s 才放一个令牌, 连续两次取数第二次会被节流成
            # None (与本次要验的缓存语义无关) —— 这里放宽成全放行
            market_mod._depth_bucket = mock.Mock(try_acquire=mock.Mock(return_value=True))
            before = datetime(2026, 9, 18, 14, 56, 30)
            with mock.patch.object(market_mod.market_hours, "now", return_value=before), \
                 mock.patch.object(market_mod.market_hours, "is_live", return_value=True):
                first = market_mod.fetch_depth("588200.SH")
            self.assertTrue(market_mod._depth_day_cache_date, "14:56 的完整五档入库")
            market_mod._depth_cache._cache.clear()
            auction = datetime(2026, 9, 18, 14, 59, 14)
            with mock.patch.object(market_mod.market_hours, "now", return_value=auction), \
                 mock.patch.object(market_mod.market_hours, "is_live", return_value=True):
                live = market_mod.fetch_depth("588200.SH")
        # 实时下发的是竞价当时的盘口, 空档位已是 None (不再画出 0 价)
        self.assertEqual(live["bid_prices"], [1.184, None, None, None, None])
        self.assertEqual(live["bid_volumes"], [174867, None, None, None, None])
        # 当日快照没被这份残缺盘口顶掉
        replay = market_mod._depth_day_cache["588200.SH"]
        self.assertEqual(replay["bid_prices"], first["bid_prices"])
        self.assertEqual(replay["bid_volumes"], first["bid_volumes"])

    def test_stale_disk_entry_with_zero_levels_is_cleaned_on_read(self):
        """修复前写盘的补零盘口, 读出时清洗 (不靠"等它自己过期"来治)。"""
        market_mod._depth_day_cache_date = "2026-09-18"
        market_mod._depth_day_cache["588200.SH"] = {
            "symbol": "588200.SH", "timestamp": 1789714754000,
            "bid_prices": [1.184, 0.0, 0.0, 0.0, 0.0],
            "bid_volumes": [174867, 656, 0, 0, 0],
            "ask_prices": [1.184, 0.0, 0.0, 0.0, 0.0],
            "ask_volumes": [174867, 0, 0, 0, 0],
            "_revision": 1,
        }
        market_mod._depth_day_cache["000001.SZ"] = {
            "symbol": "000001.SZ", "timestamp": 1,
            "bid_prices": [0.0] * 5, "bid_volumes": [0] * 5,
            "ask_prices": [0.0] * 5, "ask_volumes": [0] * 5,
            "_revision": 1,
        }
        saturday = datetime(2026, 9, 19, 12, 0)
        with mock.patch.object(market_mod.market_hours, "now", return_value=saturday), \
             mock.patch.object(market_mod.market_hours, "is_trading_day", return_value=False):
            cleaned = market_mod._depth_day_get("588200.SH")
            self.assertEqual(cleaned["bid_prices"], [1.184, None, None, None, None])
            self.assertEqual(cleaned["_revision"], 1, "清洗不动时间戳等其它字段")
            self.assertIsNone(market_mod._depth_day_get("000001.SZ"), "全 0 的旧条目直接丢弃")

    def test_next_trading_day_expires_previous_depth(self):
        af = self._af()
        friday = datetime(2026, 9, 11, 14, 30)
        with mock.patch.object(market_mod, "AF_API_KEY", "k"), \
             mock.patch.object(market_mod, "get_af", return_value=af), \
             mock.patch.object(market_mod.market_hours, "now", return_value=friday), \
             mock.patch.object(market_mod.market_hours, "is_trading_day", return_value=True), \
             mock.patch.object(market_mod.market_hours, "is_live", return_value=True):
            market_mod.fetch_depth("002472.SZ")
        market_mod._depth_cache._cache.clear()
        monday = datetime(2026, 9, 14, 9, 0)
        with mock.patch.object(market_mod.market_hours, "now", return_value=monday), \
             mock.patch.object(market_mod.market_hours, "is_trading_day", return_value=True), \
             mock.patch.object(market_mod.market_hours, "is_live", return_value=False):
            self.assertIsNone(market_mod._depth_day_get("002472.SZ"))


class DepthCollapsedBookTest(unittest.TestCase):
    """塌陷盘口不入当日快照 —— 与时段无关的第二道判据 (`_is_collapsed_depth`)。

    第一道是时段 (`depth_book_replayable`, 见 test_market_hours.ClosingAuctionTest);
    这道按**形态**兜底, 覆盖上游在任何时刻给出的"不是五档"的东西 (偶发单档、临停复牌)。
    用例里的 payload 是**清洗后**的形状 (空档位已 None), 与 `_depth_day_set` 收到的一致。
    """

    def setUp(self):
        market_mod._depth_cache._cache.clear()
        market_mod._depth_day_cache.clear()
        market_mod._depth_day_cache_date = None
        market_mod._depth_bucket = mock.Mock(try_acquire=mock.Mock(return_value=True))
        fake_file = mock.Mock()
        fake_file.read_text.side_effect = FileNotFoundError()
        self.file_patch = mock.patch.object(market_mod, "_DEPTH_DAY_CACHE_FILE", fake_file)
        self.file_patch.start()
        self.addCleanup(self.file_patch.stop)
        perf.reset_counters()

    def test_is_collapsed_depth_truth_table(self):
        def book(bids, asks):
            return {"bid_prices": bids, "ask_prices": asks,
                    "bid_volumes": [1] * len(bids), "ask_volumes": [1] * len(asks)}

        cases = [
            ("正常五档",
             book([10.0, 9.99, 9.98, 9.97, 9.96], [10.01, 10.02, 10.03, 10.04, 10.05]), False),
            ("同价塌陷 (竞价虚拟价挂两侧)",
             book([1.184, None, None, None, None], [1.184, None, None, None, None]), True),
            ("同价带浮点噪声也算塌陷",
             book([1.184, None], [1.1840000000000002, None]), True),
            ("相邻档的浮点噪声不是塌陷",
             book([17.650000000000002, 17.64], [17.66, 17.67]), False),
            ("有效档只有 1 个",
             book([10.0, None, None, None, None], [None] * 5), True),
            # 封板票一侧全空 (上游补 0 → 清洗成 None), 有效档数正常, 必须能入库
            ("涨停单边空",
             book([10.0, 9.99, 9.98, 9.97, 9.96], [None] * 5), False),
            ("跌停单边空",
             book([None] * 5, [10.0, 10.01, 10.02, 10.03, 10.04]), False),
            ("空盘口", book([], []), True),
        ]
        for label, payload, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(market_mod._is_collapsed_depth(payload), expected)
        self.assertTrue(market_mod._is_collapsed_depth(None), "非 dict 一律当塌陷")

    def _fetch_at(self, hhmm, payload, symbol):
        """在指定时刻取一次五档 (时段门放行: 连续竞价 + is_live)。"""
        af = mock.Mock()
        af.depth.get.return_value = payload
        when = datetime(2026, 9, 18, hhmm[0], hhmm[1])
        with mock.patch.object(market_mod, "AF_API_KEY", "k"), \
             mock.patch.object(market_mod, "get_af", return_value=af), \
             mock.patch.object(market_mod.market_hours, "now", return_value=when), \
             mock.patch.object(market_mod.market_hours, "is_trading_day", return_value=True), \
             mock.patch.object(market_mod.market_hours, "is_live", return_value=True):
            return market_mod.fetch_depth(symbol)

    def test_collapsed_book_rejected_in_regular_session(self):
        """时段门放行 (14:30 是连续竞价) 但形态塌陷 —— 第二道判据必须挡住。"""
        out = self._fetch_at((14, 30), {
            "symbol": "588200.SH", "timestamp": 1789714754000,
            "bid_prices": [1.184, 0.0, 0.0, 0.0, 0.0],
            "bid_volumes": [174867, 656, 0, 0, 0],
            "ask_prices": [1.184, 0.0, 0.0, 0.0, 0.0],
            "ask_volumes": [174867, 0, 0, 0, 0],
        }, "588200.SH")
        # 实时下发不变 (仍是当时的真相, 只是空档位已 None)
        self.assertEqual(out["bid_prices"], [1.184, None, None, None, None])
        self.assertNotIn("588200.SH", market_mod._depth_day_cache, "塌陷盘口不得进当日快照")
        counters = perf.counters()
        self.assertEqual(counters.get("depth_day_reject"), 1)
        self.assertIsNone(counters.get("depth_day_write"))

    def test_one_sided_limit_up_book_still_cached(self):
        """涨停封板 (卖侧全空) 必须照常入库 —— 判据写错会让所有封板票没有当日快照。"""
        out = self._fetch_at((14, 30), {
            "symbol": "603169.SH", "timestamp": 1789714574000,
            "bid_prices": [10.0, 9.99, 9.98, 9.97, 9.96],
            "bid_volumes": [1, 2, 3, 4, 5],
            "ask_prices": [0.0] * 5, "ask_volumes": [0] * 5,
        }, "603169.SH")
        self.assertEqual(out["ask_prices"], [None] * 5)
        self.assertIn("603169.SH", market_mod._depth_day_cache)
        counters = perf.counters()
        self.assertEqual(counters.get("depth_day_write"), 1)
        self.assertIsNone(counters.get("depth_day_reject"))


class DepthStaleSnapshotRefreshTest(unittest.TestCase):
    """盘后/午休读到"早上看过的"那份快照时, 花一个令牌回源换一份更接近收盘的。

    实测盘后上游给的就是当天最后一份连续竞价盘口 (000070.SZ 快照 14:44 是 17.66/17.67,
    盘后上游给 17.70/17.71 且量已变), 所以这不是"多打一次上游", 是换掉手里那份旧的。
    """

    # 判据是条目自己的 _revision (抓取时刻, time_ns()//1000 微秒) 是否落在所属交易日的 15:00 之前:
    # 10:14 抓的那份 / 15:30 抓的那份 (盘后) / 盘后抓的但上游 timestamp 还停在 14:56 的那份
    # 2026-09-18 15:30 CEST 的纳秒值: 测试里把写入时刻钉死在这里, 免得断言跟着跑套件的钟点变
    CLOSE_FETCH_NS = 1789716600 * 10 ** 9
    MORNING = {
        "symbol": "300943.SZ", "timestamp": 1789697640000,
        "bid_prices": [33.13, 33.12, 33.11, 33.09, 33.06], "bid_volumes": [10, 20, 30, 40, 50],
        "ask_prices": [33.14, 33.15, 33.16, 33.17, 33.18], "ask_volumes": [60, 70, 80, 90, 100],
        "_revision": 1789697640000000,
    }
    CLOSE = {
        "symbol": "300943.SZ", "timestamp": 1789716600000,
        "bid_prices": [33.20, 33.19, 33.18, 33.17, 33.16], "bid_volumes": [11, 21, 31, 41, 51],
        "ask_prices": [33.21, 33.22, 33.23, 33.24, 33.25], "ask_volumes": [61, 71, 81, 91, 101],
        "_revision": 1789716600000000,
    }
    # 盘后抓的, 但上游 timestamp 还停在 14:56 —— 判据必须认"抓取时刻"而不是上游 timestamp,
    # 否则这种条目会被反复回源 (每个视图一个令牌)
    POST_CLOSE_PRE_STAMP = {
        "symbol": "300943.SZ", "timestamp": 1789714574000,
        "bid_prices": [33.19, 33.18, 33.17, 33.16, 33.15], "bid_volumes": [12, 22, 32, 42, 52],
        "ask_prices": [33.20, 33.21, 33.22, 33.23, 33.24], "ask_volumes": [62, 72, 82, 92, 102],
        "_revision": 1789716600000000,
    }

    def setUp(self):
        market_mod._depth_cache._cache.clear()
        market_mod._depth_day_cache.clear()
        market_mod._depth_day_cache_date = None
        market_mod._depth_bucket = mock.Mock(try_acquire=mock.Mock(return_value=True))
        fake_file = mock.Mock()
        fake_file.read_text.side_effect = FileNotFoundError()
        self.file_patch = mock.patch.object(market_mod, "_DEPTH_DAY_CACHE_FILE", fake_file)
        self.file_patch.start()
        self.addCleanup(self.file_patch.stop)
        perf.reset_counters()

    def _seed(self, symbol, payload, date="2026-09-18"):
        market_mod._depth_day_cache_date = date
        market_mod._depth_day_cache[symbol] = payload
        market_mod._depth_cache._cache.clear()

    def _af(self, payload=None, error=None):
        af = mock.Mock()
        if error:
            af.depth.get.side_effect = error
        else:
            af.depth.get.return_value = payload
        return af

    def _read(self, symbol, af, when, live=False):
        """在指定时刻读一次五档; **抓取时刻钉死**在 2026-09-18 15:30 (收盘后)。

        写入的 `_revision` 取自 `time.time_ns()`, 若用真实时钟, 落档那份的"抓取时刻"就跟着
        跑套件时的钟点变 —— 15:00 之前跑 `test_morning_snapshot_refreshed_to_close_book` 的
        "收敛"断言就会挂 (曾经如此)。这里把 time 换成 wraps 真模块的 Mock, 只钉死 time_ns,
        其余 (time.time 等) 仍走真实实现。
        """
        fake_time = mock.Mock(wraps=market_mod.time)
        fake_time.time_ns = mock.Mock(return_value=self.CLOSE_FETCH_NS)
        with mock.patch.object(market_mod, "AF_API_KEY", "k"), \
             mock.patch.object(market_mod, "get_af", return_value=af), \
             mock.patch.object(market_mod, "time", fake_time), \
             mock.patch.object(market_mod.market_hours, "now", return_value=when), \
             mock.patch.object(market_mod.market_hours, "is_trading_day",
                               return_value=when.weekday() < 5), \
             mock.patch.object(market_mod.market_hours, "is_live", return_value=live):
            return market_mod.fetch_depth(symbol)

    def test_morning_snapshot_refreshed_to_close_book(self):
        """周末读到 10:14 那份 → 回源换 15:30 那份, 并且顶掉内存与磁盘上那份。"""
        self._seed("300943.SZ", self.MORNING)
        af = self._af(self.CLOSE)
        out = self._read("300943.SZ", af, datetime(2026, 9, 19, 16, 0))
        self.assertEqual(out["timestamp"], self.CLOSE["timestamp"])
        self.assertEqual(out["bid_prices"], self.CLOSE["bid_prices"])
        af.depth.get.assert_called_once()
        self.assertEqual(market_mod._depth_day_cache["300943.SZ"]["timestamp"],
                         self.CLOSE["timestamp"], "刷新出来的那份要留在快照里 (下次不再花令牌)")
        self.assertEqual(market_mod._depth_day_cache_date, "2026-09-18", "周末刷新不改交易日 key")
        # 落档的这份是"收盘后抓的" → 之后所有视图都直接回放, 不会再换 (收敛)
        self.assertFalse(market_mod._depth_snapshot_stale(
            market_mod._depth_day_cache["300943.SZ"], market_mod._depth_day_cache_date))

    def test_weekend_morning_refresh_converges(self):
        """周末**上午**刷新出来的那份也必须收敛 (第 3 次读不该再打上游)。

        旧判据只看"时分 < 15:00", 于是周六 10:00 抓的那份永远算"收盘前抓的", 连看 3 次
        = 3 次上游调用 (每个视图一个令牌, 永不收敛)。判据必须带上日期: 抓取日期晚于所属
        交易日 (= 上一个交易日收盘之后) 就是"收盘后那份"。
        """
        self._seed("300943.SZ", self.MORNING)
        af = self._af(self.CLOSE)
        sat_morning = datetime(2026, 9, 19, 10, 0)
        for _ in range(3):
            market_mod._depth_cache._cache.clear()      # 每次都走当日快照那条路径, 不靠 TTL 缓存
            out = self._read("300943.SZ", af, sat_morning)
            self.assertEqual(out["timestamp"], self.CLOSE["timestamp"])
        self.assertEqual(af.depth.get.call_count, 1, "第一次换到收盘那份后就不该再回源")

    def test_post_close_fetched_snapshot_not_refetched(self):
        """盘后抓的那份不再回源 —— 判据是抓取时刻, 不是上游 timestamp。

        上游盘后给的 timestamp 是它自己的刷新时刻 (实测 15:30), 但那是供应商行为; 只要
        认"我们抓它的时候过了 15:00 没有", 就不会因为上游给的时间戳旧而反复回源。
        """
        self._seed("300943.SZ", self.POST_CLOSE_PRE_STAMP)
        af = self._af(self.CLOSE)
        out = self._read("300943.SZ", af, datetime(2026, 9, 19, 16, 0))
        self.assertEqual(out["timestamp"], self.POST_CLOSE_PRE_STAMP["timestamp"])
        af.depth.get.assert_not_called()

    def test_no_cache_file_on_non_trading_day_still_persists(self):
        """非交易日 + 缓存文件缺失 (全新进程/删过文件): 刷新结果要挂到最近一个交易日上。

        否则 key 是 None → `_depth_day_set` 直接 early-return → 既不落盘也不进内存,
        每次查看都要重新回源。
        """
        market_mod._depth_day_cache.clear()
        market_mod._depth_day_cache_date = None
        af = self._af(self.CLOSE)
        for _ in range(2):
            market_mod._depth_cache._cache.clear()
            out = self._read("300943.SZ", af, datetime(2026, 9, 19, 16, 0))
            self.assertEqual(out["timestamp"], self.CLOSE["timestamp"])
        self.assertEqual(market_mod._depth_day_cache_date, "2026-09-18",
                         "挂到最近一个已收盘的交易日 (周五)")
        self.assertIn("300943.SZ", market_mod._depth_day_cache)
        self.assertEqual(af.depth.get.call_count, 1, "第二次读直接用落档那份")

    def test_stored_entry_decoupled_from_returned_payload(self):
        """落档那份必须与下发给 /api/depth 的对象解耦 (否则别人就地改一下就改掉了缓存)。"""
        out = self._read("300943.SZ", self._af(self.CLOSE), datetime(2026, 9, 18, 14, 30), live=True)
        out["bid_prices"][0] = 999.0
        out["injected"] = True
        stored = market_mod._depth_day_cache["300943.SZ"]
        self.assertNotEqual(stored["bid_prices"][0], 999.0)
        self.assertNotIn("injected", stored)

    def test_corrupt_row_does_not_raise(self):
        """脏行 (非序列的档位) 只是"没有档位", 不能让 /api/depth 打 500。"""
        self._seed("BAD.SZ", {"symbol": "BAD.SZ", "timestamp": 1,
                              "bid_prices": 1.0, "bid_volumes": None,
                              "ask_prices": "x", "ask_volumes": {}, "_revision": 1})
        self.assertIsNone(market_mod._depth_day_get("BAD.SZ"))
        self.assertTrue(market_mod._is_collapsed_depth({"bid_prices": 1.0, "ask_prices": "x"}))
        self.assertEqual(market_mod._clean_depth_payload({"bid_prices": 1.0, "ask_prices": []}), None)

    def test_legacy_entry_without_revision_uses_upstream_timestamp(self):
        """老条目没有可用的 _revision → 退回上游 timestamp 判陈旧 (10:14 那份要换掉)。"""
        legacy = {k: v for k, v in self.MORNING.items() if k != "_revision"}
        legacy["_revision"] = 1     # 认不出的值
        self._seed("300943.SZ", legacy)
        af = self._af(self.CLOSE)
        out = self._read("300943.SZ", af, datetime(2026, 9, 19, 16, 0))
        self.assertEqual(out["timestamp"], self.CLOSE["timestamp"])
        af.depth.get.assert_called_once()

    def test_stale_snapshot_kept_when_upstream_unavailable(self):
        """回源失败 (AF 抛错 + 无麦蕊 key) 时退回手里那份, 不能变成空。"""
        self._seed("300943.SZ", self.MORNING)
        af = self._af(error=RuntimeError("af down"))
        out = self._read("300943.SZ", af, datetime(2026, 9, 19, 16, 0))
        self.assertEqual(out["timestamp"], self.MORNING["timestamp"])
        self.assertEqual(out["bid_prices"], self.MORNING["bid_prices"])

    def test_hk_symbol_not_refreshed(self):
        """港美股不按 A股 时段口径判陈旧 (它们的盘中在本口径里本就不是 live)。"""
        hk = {**self.MORNING, "symbol": "00700.HK"}
        self._seed("00700.HK", hk)
        af = self._af(self.CLOSE)
        out = self._read("00700.HK", af, datetime(2026, 9, 19, 16, 0))
        self.assertEqual(out["timestamp"], self.MORNING["timestamp"])
        af.depth.get.assert_not_called()

    def test_collapsed_snapshot_also_refreshed(self):
        """修复前写盘的塌陷条目 (线上那条 14:59 补零盘口) 也要被换掉。

        它按时刻算是"收盘尾巴", 但内容根本不是五档 —— 不回源就会把竞价那一刻摆一晚上。
        """
        self._seed("588200.SH", {
            "symbol": "588200.SH", "timestamp": 1789714754000,
            "bid_prices": [1.184, None, None, None, None], "bid_volumes": [174867, None, None, None, None],
            "ask_prices": [1.184, None, None, None, None], "ask_volumes": [174867, None, None, None, None],
            "_revision": 1,
        })
        af = self._af(self.CLOSE)
        out = self._read("588200.SH", af, datetime(2026, 9, 19, 16, 0))
        self.assertEqual(out["timestamp"], self.CLOSE["timestamp"])
        af.depth.get.assert_called_once()

    def test_older_book_does_not_overwrite_newer(self):
        """一次滞后的响应不该把更好的那份顶掉 (只新不旧)。"""
        self._seed("300943.SZ", self.POST_CLOSE_PRE_STAMP)
        stale_af = self._af({**self.MORNING, "timestamp": 1789713000000})  # 14:30
        out = self._read("300943.SZ", stale_af, datetime(2026, 9, 18, 14, 56), live=True)
        self.assertEqual(out["timestamp"], 1789713000000, "下发的仍是这次取到的值")
        self.assertEqual(market_mod._depth_day_cache["300943.SZ"]["timestamp"],
                         self.POST_CLOSE_PRE_STAMP["timestamp"], "但快照没被这份更旧的顶掉")
        self.assertEqual(perf.counters().get("depth_day_stale_skip"), 1)


if __name__ == "__main__":
    unittest.main()
