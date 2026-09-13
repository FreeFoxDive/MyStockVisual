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

    def test_obv_and_volume_ma_parity_full_series(self):
        """OBV 为全量累加, 必须用全量 Python 口径对照 (不能用 tail 切片)。"""
        require_node()
        import pandas as pd
        from indicators import compute_all_indicators

        klines = make_kline_fixture(120, seed=9)
        df = pd.DataFrame(klines).set_index(pd.to_datetime([k["date"] for k in klines]))
        out, _ = compute_all_indicators(df, period="1d")
        keys = ("obv", "maobv", "vol_ma5", "vol_ma10", "vol_ma20")
        py = {key: (None if pd.isna(out.iloc[-1][key]) else float(out.iloc[-1][key]))
              for key in keys}
        js = recalc_tail_js(copy.deepcopy(klines), "1d")
        for key in keys:
            self.assertIsNotNone(js.get(key), msg=key)
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

    def test_appended_bar_gets_all_indicator_fields(self):
        """回归: 快照追加的当日 bar 只有 OHLCV, recalc 后必须补齐 BOLL/WR/CCI/BIAS/DMI。

        否则图例 paintLegend 读到 undefined → 显示 —/—/— (BOLL 开关切换触发重绘时暴露)。
        """
        require_node()
        klines = make_kline_fixture(80, seed=13)
        klines.append({
            "date": "2026-12-31",
            "open": 105.0, "high": 106.0, "low": 104.0, "close": 105.5,
            "volume": 2_000_000, "amount": 2_000_000 * 105.5,
        })
        js = recalc_tail_js(copy.deepcopy(klines), "1d")
        for key in ("boll_mid", "boll_up", "boll_low", "wr14", "cci14",
                    "bias6", "bias12", "bias24", "dmi_pdi", "dmi_mdi", "dmi_adx"):
            self.assertIsNotNone(js.get(key), msg=f"追加 bar 后 {key} 为空 (图例会显示 —)")


if __name__ == "__main__":
    unittest.main()
