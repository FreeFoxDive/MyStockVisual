# -*- coding: utf-8 -*-
"""ETF 溢价回填: applyPremiumToKlines 从 static/index.html 抽取真实函数测试。

契约: /api/kline 不再内联 premium (akshare 净值慢), 改由 meta.deferred 声明后
异步补齐; 前端按 **date** 回填, 与 bar 数量/顺序解耦, 且任何整包替换
(STATE.klineData = data) 之后都能靠内存副本复原。

运行:
    venv/Scripts/python.exe -u visual/test/test_premium_js.py
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
_TEST_DIR = Path(__file__).resolve().parent
for p in (str(_VISUAL_DIR), str(_TEST_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from js_test_util import require_node, run_node  # noqa: E402

INDEX_HTML = _VISUAL_DIR / "static" / "index.html"


class PremiumJsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_node()
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _extract(self, name):
        m = re.search(r"^function %s\(.*?\n\}" % re.escape(name), self.src, re.S | re.M)
        self.assertIsNotNone(m, f"index.html 中未找到函数 {name}")
        return m.group(0)

    def _run(self, klines, premium, symbol="510300.SH", period="1d"):
        state = {"symbol": symbol, "period": period,
                 "klineData": {"symbol": symbol, "period": period, "klines": klines},
                 "premium": premium}
        js = (
            "let STATE = JSON.parse(process.argv[1]);\n"
            + self._extract("applyPremiumToKlines") + "\n"
            + "const changed = applyPremiumToKlines();\n"
            + "process.stdout.write(JSON.stringify({changed, klines: STATE.klineData.klines}));"
        )
        return json.loads(run_node(js, json.dumps(state)))

    def test_fills_premium_by_date(self):
        """整包替换后 (bars 无 premium) 用内存 map 按 date 复原。"""
        klines = [{"date": "2026-09-10", "close": 4.0},
                  {"date": "2026-09-11", "close": 4.1}]
        out = self._run(klines, {"symbol": "510300.SH", "period": "1d",
                                 "map": {"2026-09-10": 1.25, "2026-09-11": -0.5}})
        self.assertTrue(out["changed"])
        self.assertEqual(out["klines"][0]["premium"], 1.25)
        self.assertEqual(out["klines"][1]["premium"], -0.5)

    def test_null_value_is_applied_not_skipped(self):
        """净值缺失的交易日应为显式 null (界面显示 —), 而不是沿用旧值。"""
        klines = [{"date": "2026-09-11", "close": 4.1, "premium": 9.9}]
        out = self._run(klines, {"symbol": "510300.SH", "period": "1d",
                                 "map": {"2026-09-11": None}})
        self.assertTrue(out["changed"])
        self.assertIsNone(out["klines"][0]["premium"])

    def test_date_not_in_map_left_untouched(self):
        """map 未覆盖的日期不写入 (保持缺失), 避免把 null 当溢价填满全序列。"""
        klines = [{"date": "2026-09-11", "close": 4.1}]
        out = self._run(klines, {"symbol": "510300.SH", "period": "1d",
                                 "map": {"2026-09-10": 1.25}})
        self.assertFalse(out["changed"])
        self.assertNotIn("premium", out["klines"][0])

    def test_idempotent_second_apply_is_noop(self):
        klines = [{"date": "2026-09-11", "close": 4.1, "premium": 1.25}]
        out = self._run(klines, {"symbol": "510300.SH", "period": "1d",
                                 "map": {"2026-09-11": 1.25}})
        self.assertFalse(out["changed"], "值未变化不应触发重绘")

    def test_symbol_mismatch_skipped(self):
        """切股瞬间旧 symbol 的溢价不得涂到新 symbol 的图上。"""
        klines = [{"date": "2026-09-11", "close": 4.1}]
        out = self._run(klines, {"symbol": "510050.SH", "period": "1d",
                                 "map": {"2026-09-11": 1.25}})
        self.assertFalse(out["changed"])
        self.assertNotIn("premium", out["klines"][0])

    def test_period_mismatch_skipped(self):
        klines = [{"date": "2026-09-11", "close": 4.1}]
        out = self._run(klines, {"symbol": "510300.SH", "period": "1w",
                                 "map": {"2026-09-11": 1.25}})
        self.assertFalse(out["changed"])

    def test_no_premium_state_is_noop(self):
        klines = [{"date": "2026-09-11", "close": 4.1}]
        self.assertFalse(self._run(klines, None)["changed"])

    def test_source_fetches_deferred_endpoint(self):
        """前端必须真的去拉补齐接口, 否则溢价会永久缺席。"""
        self.assertIn("/api/kline/deferred", self.src)
        self.assertIn("meta.deferred", self.src)


if __name__ == "__main__":
    unittest.main()
