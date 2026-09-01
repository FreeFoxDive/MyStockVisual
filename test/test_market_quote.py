"""fetch_quotes AF 优先 + 麦蕊回退单测。"""
from __future__ import annotations

import os
import sys
import unittest
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


class TestFetchQuotesAfFirst(unittest.TestCase):
    def setUp(self):
        market_mod.quote_cache._cache.clear()
        self.p_index = mock.patch.object(market_mod, "_is_index_symbol", side_effect=_is_index)
        self.p_etf = mock.patch.object(market_mod, "_is_etf", side_effect=_is_etf)
        self.p_index.start()
        self.p_etf.start()

    def tearDown(self):
        self.p_index.stop()
        self.p_etf.stop()

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
        api.fund_real_time.return_value = _mr_row()
        with mock.patch.object(market_mod, "_fetch_af_quotes", return_value={}):
            with mock.patch.object(market_mod, "get_mr", return_value=api):
                out = market_mod.fetch_quotes(["510300.SH"], fresh=True)
        api.fund_real_time.assert_called_once_with("510300")
        self.assertEqual(out["510300.SH"]["last_price"], 10.5)

    def test_stock_fallback_mairui_when_af_empty(self):
        api = mock.Mock()
        api.stock_ssjy_more.return_value = [{"dm": "600519", **_mr_row()}]
        with mock.patch.object(market_mod, "_fetch_af_quotes", return_value={}):
            with mock.patch.object(market_mod, "get_mr", return_value=api):
                out = market_mod.fetch_quotes(["600519.SH"], fresh=True)
        api.stock_ssjy_more.assert_called_once()
        self.assertEqual(out["600519.SH"]["last_price"], 10.5)

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

    def test_af_quote_to_std_maps_fields(self):
        raw = _af_row("600519.SH", name="茅台", change_pct=0.05)
        with mock.patch.object(market_mod, "_lookup_name", return_value="备用名"):
            out = market_mod._af_quote_to_std(raw, "600519.SH")
        self.assertEqual(out["last_price"], 10.5)
        self.assertEqual(out["name"], "茅台")
        self.assertEqual(out["change_pct"], 0.05)
        raw2 = _af_row("600519.SH", name=None)
        with mock.patch.object(market_mod, "_lookup_name", return_value="备用名"):
            out2 = market_mod._af_quote_to_std(raw2, "600519.SH")
        self.assertEqual(out2["name"], "备用名")

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


if __name__ == "__main__":
    unittest.main()
