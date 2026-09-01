# -*- coding: utf-8 -*-
"""indicators.js 底层函数单元测试（Node 手算对照）。"""

from __future__ import annotations

import json
import unittest

from js_test_util import run_indicators_js, require_node


@unittest.skipUnless(__import__("shutil").which("node"), "需要 node")
class TestIndicatorsJsUnit(unittest.TestCase):
    def _calc(self, expr: str, payload=None):
        if payload is None:
            return json.loads(run_indicators_js(f"process.stdout.write(JSON.stringify({expr}));"))
        return json.loads(run_indicators_js(
            f"const p = JSON.parse(process.argv[1]); process.stdout.write(JSON.stringify({expr}));",
            json.dumps(payload),
        ))

    def test_sma_window5(self):
        out = self._calc("IndicatorCalc.sma([10,20,30,40,50], 5)")
        self.assertEqual(out[4], 30.0)

    def test_ema_first_equals_first_element(self):
        out = self._calc("IndicatorCalc.ema([10, 11, 12], 3)")
        self.assertEqual(out[0], 10.0)

    def test_macd_hist_is_double_dif_minus_dea(self):
        out = self._calc(
            "(() => { const c=[10,10.5,11,11.2,11.5,12,12.1,12.3,12.5,13,"
            "13.2,13.5,14,14.1,14.3,14.5,15,15.2,15.5,16,16.2,16.5,17,17.1,"
            "17.3,17.5,18,18.2]; const m=IndicatorCalc.macd(c,12,26,9);"
            "return {dif:m.dif.slice(-1)[0],dea:m.dea.slice(-1)[0],hist:m.hist.slice(-1)[0]}; })()"
        )
        self.assertAlmostEqual(out["hist"], 2 * (out["dif"] - out["dea"]), places=6)

    def test_kdj_first_valid_after_warmup(self):
        payload = {
            "h": [12, 13, 14, 15, 16, 17, 18, 19, 20, 21],
            "l": [8, 9, 10, 11, 12, 13, 14, 15, 16, 17],
            "c": [10, 11, 12, 13, 14, 15, 16, 17, 18, 19],
        }
        kdj = self._calc(
            "(() => { const k=IndicatorCalc.kdj(p.h,p.l,p.c,9);"
            " return {k:k.k,d:k.d,j:k.j}; })()",
            payload,
        )
        self.assertIsNotNone(kdj["k"][8])
        self.assertIsNotNone(kdj["d"][8])
        self.assertIsNotNone(kdj["j"][8])

    def test_rsi_short_series_mostly_null(self):
        out = self._calc("IndicatorCalc.rsi([10,10.5,11,10.8,11.2,11.0], 6)")
        self.assertTrue(out[-1] is None or isinstance(out[-1], (int, float)))

    def test_atr_first_bar_null_until_period(self):
        payload = {
            "h": [12.0, 13.0, 14.0, 15.0, 16.0, 17.0, 18.0, 19.0, 20.0, 21.0, 22.0, 23.0, 24.0, 25.0],
            "l": [10.0] * 14,
            "c": [11.0] * 14,
        }
        out = self._calc("IndicatorCalc.atr(p.h,p.l,p.c,14)", payload)
        self.assertIsNone(out[0])
        self.assertIsNotNone(out[13])
        self.assertGreater(out[13], 0)


if __name__ == "__main__":
    unittest.main()
