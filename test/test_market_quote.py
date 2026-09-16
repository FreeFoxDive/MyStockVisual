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
      * `in_session` 显式为真 —— 这些用例测的是上游五档的字段映射与回退。
    """
    if fresh:
        market_mod._depth_cache._cache.clear()
    market_mod._depth_day_cache.clear()
    market_mod._depth_day_cache_date = None
    market_mod._depth_bucket = None
    fake_file = mock.Mock()
    fake_file.read_text.side_effect = FileNotFoundError()
    with mock.patch.object(market_mod, "_DEPTH_DAY_CACHE_FILE", fake_file), \
         mock.patch.object(market_mod.market_hours, "in_session", return_value=True):
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
    """盘后长 TTL 最后快照: 页面跳转不再因预算/锁抖动返回 {}。"""

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
        with mock.patch.object(market_mod.market_hours, "in_session", return_value=False), \
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
            with mock.patch.object(market_mod.market_hours, "in_session", return_value=False):
                out = market_mod.fetch_quotes(["600519.SH"])
        finally:
            market_mod._quote_fetch_lock.release()
        self.assertEqual(out["600519.SH"]["last_price"], 9.9)

    def test_off_session_budget_denied_returns_snapshot(self):
        market_mod.quote_snapshot_cache.set("600519.SH", _std_quote("600519.SH", 9.9))
        with mock.patch.object(market_mod.market_hours, "in_session", return_value=False), \
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
        with mock.patch.object(market_mod.market_hours, "in_session", return_value=False):
            out = market_mod.fetch_quotes(["600519.SH"])
        self.assertEqual(out["600519.SH"]["last_price"], 10.5)

    def test_in_session_ignores_snapshot(self):
        market_mod.quote_snapshot_cache.set("600519.SH", _std_quote("600519.SH", 99.0))
        with mock.patch.object(market_mod.market_hours, "in_session", return_value=True), \
             mock.patch.object(market_mod, "_fetch_af_quotes",
                               return_value={"600519.SH": _af_row("600519.SH")}), \
             mock.patch.object(market_mod, "get_mr", return_value=mock.Mock()) as get_mr:
            out = market_mod.fetch_quotes(["600519.SH"])
        self.assertEqual(out["600519.SH"]["last_price"], 10.5)
        self.assertNotEqual(out["600519.SH"]["last_price"], 99.0)

    def test_in_session_no_upstream_no_snapshot_fallback(self):
        market_mod.quote_snapshot_cache.set("600519.SH", _std_quote("600519.SH", 99.0))
        with mock.patch.object(market_mod.market_hours, "in_session", return_value=True), \
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
             mock.patch.object(market_mod.market_hours, "in_session", return_value=True):
            first = market_mod.fetch_depth("002472.SZ")
        market_mod._depth_cache._cache.clear()
        saturday = datetime(2026, 9, 12, 12, 0)
        with mock.patch.object(market_mod.market_hours, "now", return_value=saturday), \
             mock.patch.object(market_mod.market_hours, "is_trading_day", return_value=False), \
             mock.patch.object(market_mod.market_hours, "in_session", return_value=False):
            self.assertEqual(market_mod.fetch_depth("002472.SZ"), first)
        af.depth.get.assert_called_once()

    def test_next_trading_day_expires_previous_depth(self):
        af = self._af()
        friday = datetime(2026, 9, 11, 14, 30)
        with mock.patch.object(market_mod, "AF_API_KEY", "k"), \
             mock.patch.object(market_mod, "get_af", return_value=af), \
             mock.patch.object(market_mod.market_hours, "now", return_value=friday), \
             mock.patch.object(market_mod.market_hours, "is_trading_day", return_value=True), \
             mock.patch.object(market_mod.market_hours, "in_session", return_value=True):
            market_mod.fetch_depth("002472.SZ")
        market_mod._depth_cache._cache.clear()
        monday = datetime(2026, 9, 14, 9, 0)
        with mock.patch.object(market_mod.market_hours, "now", return_value=monday), \
             mock.patch.object(market_mod.market_hours, "is_trading_day", return_value=True), \
             mock.patch.object(market_mod.market_hours, "in_session", return_value=False):
            self.assertIsNone(market_mod._depth_day_get("002472.SZ"))


if __name__ == "__main__":
    unittest.main()
