# -*- coding: utf-8 -*-
"""末根指标重算：Python compute_all_indicators 与 indicators.js 对照。"""

from __future__ import annotations

import copy
import unittest

from js_test_util import (
    INDICATOR_KEYS,
    assert_js_py_parity,
    make_kline_fixture,
    python_last_indicators,
    recalc_tail_js,
    require_node,
)


class TestRecalcTailIndicators(unittest.TestCase):
    def test_all_keys_match_fixture_80(self):
        klines = make_kline_fixture(80)
        assert_js_py_parity(self, klines, period="1d", places=4)

    def test_js_updates_after_quote_patch(self):
        klines = make_kline_fixture(80)
        last = klines[-1]
        last["close"] = last["close"] + 2.5
        last["high"] = max(last["high"], last["close"])
        last["volume"] = last["volume"] + 100_000
        assert_js_py_parity(self, klines, period="1d", places=4)

    def test_short_history_30_bars(self):
        klines = make_kline_fixture(30, seed=7)
        assert_js_py_parity(self, klines, period="1d", places=4)

    def test_tail_window_60_when_long(self):
        require_node()
        klines = make_kline_fixture(100, seed=11)
        mid = 50
        old_close = klines[mid]["close"]
        klines[mid]["close"] = old_close + 99.0
        klines[mid]["high"] = max(klines[mid]["high"], klines[mid]["close"])
        before_mid = copy.deepcopy(klines[mid])
        k_copy = copy.deepcopy(klines)
        recalc_tail_js(k_copy, "1d")
        self.assertEqual(k_copy[mid]["close"], klines[mid]["close"])
        for key in ("ma5", "macd_dif", "kdj_k"):
            if before_mid.get(key) is not None:
                self.assertEqual(k_copy[mid].get(key), before_mid.get(key), msg=key)

    def test_weekly_macd_params(self):
        klines = make_kline_fixture(80, seed=3)
        assert_js_py_parity(self, klines, period="1w", places=4)
        py = python_last_indicators(klines, period="1w")
        js = recalc_tail_js(copy.deepcopy(klines), "1w")
        for key in ("macd_dif", "macd_dea", "macd_hist"):
            if py[key] is not None:
                self.assertAlmostEqual(js[key], py[key], places=4, msg=key)

    def test_append_new_bar_recalc(self):
        require_node()
        klines = make_kline_fixture(80, seed=5)
        last_date = klines[-1]["date"]
        import pandas as pd
        next_date = (pd.Timestamp(last_date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        klines.append({
            "date": next_date,
            "open": 105.0,
            "high": 106.0,
            "low": 104.0,
            "close": 105.5,
            "volume": 2_000_000,
            "amount": 2_000_000 * 105.5,
        })
        assert_js_py_parity(self, klines, period="1d", places=4)


if __name__ == "__main__":
    unittest.main()
