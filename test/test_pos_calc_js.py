# -*- coding: utf-8 -*-
"""仓位计算纯函数 + 主页接线（Node）。

默认 10 万 / 风险 2% / 止损 2×ATR / 100 股向下取整。
运行:
    venv/Scripts/python.exe -u visual/test/test_pos_calc_js.py
"""
from __future__ import annotations

import json
import os
import unittest

from js_test_util import require_node, run_node

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
VISUAL_DIR = os.path.dirname(TEST_DIR)
POS_JS = os.path.join(VISUAL_DIR, "static", "js", "pos-calc.js").replace("\\", "/")
INDEX_HTML = os.path.join(VISUAL_DIR, "static", "index.html")


def _calc(payload):
    require_node()
    script = (
        f"const P = require({json.dumps(POS_JS)});\n"
        "const c = JSON.parse(process.argv[1]);\n"
        "const out = { cfg: P.normalizePosCalcConfig(c.cfg || null) };\n"
        "if (c.sizing) out.sizing = P.calcPositionSizing(c.sizing);\n"
        "if (c.side) out.side = P.formatSideRows(c.side);\n"
        "process.stdout.write(JSON.stringify(out));"
    )
    return json.loads(run_node(script, json.dumps(payload)))


class PosCalcMathTest(unittest.TestCase):
    def test_defaults(self):
        out = _calc({})
        self.assertEqual(out["cfg"], {
            "capital": 100000, "riskPct": 2, "atrMult": 2, "atrPeriod": 14,
        })

    def test_boolean_is_not_a_number(self):
        out = _calc({"cfg": {"capital": True, "riskPct": True, "atrMult": True, "atrPeriod": True}})
        self.assertEqual(out["cfg"], {
            "capital": 100000, "riskPct": 2, "atrMult": 2, "atrPeriod": 14,
        })

    def test_normalize_rejects_out_of_range(self):
        out = _calc({"cfg": {"capital": 10, "riskPct": 80, "atrMult": 0, "atrPeriod": 30}})
        self.assertEqual(out["cfg"]["capital"], 100000)
        self.assertEqual(out["cfg"]["riskPct"], 2)
        self.assertEqual(out["cfg"]["atrMult"], 2)
        self.assertEqual(out["cfg"]["atrPeriod"], 14)
        kept = _calc({"cfg": {"capital": 200000, "riskPct": 1.5, "atrMult": 2.5, "atrPeriod": 20}})
        self.assertEqual(kept["cfg"], {
            "capital": 200000, "riskPct": 1.5, "atrMult": 2.5, "atrPeriod": 20,
        })

    def test_rounds_shares_down_to_lot(self):
        # 止损距离 0.201, 2000/0.201 = 9950.25 → 9900 股, 不能进到 10000
        out = _calc({"sizing": {"entryPrice": 10, "atr": 0.1005, "atrMult": 2}})["sizing"]
        self.assertEqual(out["shares"], 9900)
        self.assertEqual(out["lots"], 99)
        self.assertEqual(out["byCapShares"], 10000)
        self.assertTrue(out["ok"])
        self.assertIsNone(out["note"])
        self.assertEqual(out["stopPrice"], 9.8)

    def test_capital_cap_and_too_small(self):
        capped = _calc({"sizing": {"entryPrice": 10, "atr": 0.01}})["sizing"]
        self.assertEqual(capped["shares"], 10000)
        self.assertEqual(capped["byRiskShares"], 100000)
        self.assertEqual(capped["note"], "受本金上限约束")
        self.assertEqual(capped["posPct"], 100)

        risk = _calc({"sizing": {"entryPrice": 100, "atr": 15}})["sizing"]
        self.assertEqual(risk["shares"], 0)
        self.assertEqual(risk["note"], "不足1手（超风险预算）")
        self.assertFalse(risk["ok"])

        cash = _calc({"sizing": {"entryPrice": 200000, "atr": 1}})["sizing"]
        self.assertEqual(cash["shares"], 0)
        self.assertEqual(cash["note"], "不足1手（超本金）")

    def test_etf_stop_uses_three_decimals_and_index_hides_rows(self):
        etf = _calc({"sizing": {"entryPrice": 1.234, "atr": 0.01, "isEtf": True}})["sizing"]
        self.assertEqual(etf["dp"], 3)
        self.assertEqual(etf["stopPrice"], 1.214)
        side = _calc({"side": {
            "isEtf": True, "entryPrice": 1.234, "atr14": 0.01, "atr20": 0.02,
            "downColor": "down",
        }})["side"]
        self.assertEqual(side[1]["text"], "1.214")
        self.assertEqual(side[1]["color"], "down")
        self.assertEqual(side[0]["action"], "poscalc")

        hidden = _calc({"side": {
            "isIndex": True, "entryPrice": 3000, "atr14": 20, "atr20": 18,
        }})["side"]
        self.assertEqual(hidden, [])
        missing = _calc({"side": {"entryPrice": 10, "atr14": None, "atr20": 0.4}})["side"]
        self.assertEqual(missing, [])

    def test_atr_period_switch(self):
        common = {"entryPrice": 10, "atr14": 1, "atr20": 0.5, "downColor": "down"}
        a14 = _calc({"side": dict(common, config={"atrPeriod": 14})})["side"]
        a20 = _calc({"side": dict(common, config={"atrPeriod": 20})})["side"]
        self.assertEqual(a14[0]["text"], "10手 (10%)")
        self.assertEqual(a14[1]["text"], "8.00")
        self.assertIn("ATR14", a14[1]["title"])
        self.assertIn("点击修改算仓参数", a14[0]["title"])
        self.assertEqual(a20[0]["text"], "20手 (20%)")
        self.assertEqual(a20[1]["text"], "9.00")
        self.assertIn("ATR20", a20[1]["title"])

    def test_stop_below_zero_is_invalid(self):
        out = _calc({"sizing": {"entryPrice": 10, "atr": 6}})["sizing"]
        self.assertEqual(out["reason"], "stop_non_positive")
        self.assertFalse(out["ok"])
        side = _calc({"side": {"entryPrice": 10, "atr14": 6}})["side"]
        self.assertEqual(side, [])


class PosCalcPageWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = open(INDEX_HTML, encoding="utf-8").read()

    def test_toolbar_modal_and_script(self):
        self.assertIn('id="btn-poscalc"', self.src)
        btn = self.src[self.src.index('id="btn-poscalc"'):self.src.index('id="btn-poscalc"') + 80]
        self.assertIn('class="icon-btn"', btn)
        self.assertIn('id="poscalc-modal"', self.src)
        self.assertIn('src="/js/pos-calc.js"', self.src)
        self.assertIn("VisualPosCalc.formatSideRows", self.src)
        self.assertIn('data-action=', self.src)
        self.assertIn("function wirePosCalc()", self.src)
        self.assertIn("function openPosCalcModal()", self.src)
        self.assertIn("entryEl.value = px != null ? String(px) : ''", self.src)
        self.assertIn("atrEl.value = followedAtr != null ? String(followedAtr) : ''", self.src)
        self.assertIn("function posCalcInputIssue()", self.src)
        self.assertIn("info.symbol === STATE.symbol", self.src)
        self.assertIn("晚到的账号参数要补一次", self.src)
        # 算仓参数不是勾选态, 手机独立缓存不能把它从共享配置里拆走
        keys = self.src[self.src.index("const CFG_CHECK_KEYS"):self.src.index("function splitCfg")]
        self.assertNotIn("posCalc", keys)
        self.assertIn("posCalc:", self.src[self.src.index("function buildConfig"):self.src.index("function saveConfig")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
