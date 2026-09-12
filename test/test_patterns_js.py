# -*- coding: utf-8 -*-
"""patterns.js 单元测试（Node）: K 线形态识别。"""

from __future__ import annotations

import json
import os
import shutil
import unittest

from js_test_util import require_node, run_node

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
VISUAL_DIR = os.path.dirname(TEST_DIR)
PATTERNS_JS = os.path.join(VISUAL_DIR, "static", "js", "patterns.js").replace("\\", "/")


def run_expr(expr: str) -> dict:
    require_node()
    if not (os.path.isfile(PATTERNS_JS) or os.path.isfile(PATTERNS_JS.replace("/", os.sep))):
        raise unittest.SkipTest("patterns.js missing")
    script = f"""
const PS = require({json.dumps(PATTERNS_JS)});
process.stdout.write(JSON.stringify({expr}));
"""
    return json.loads(run_node(script))


def bar(o, c, h, l):
    return {"open": o, "high": h, "low": l, "close": c, "volume": 1000}


def named(bars, start=1):
    return [dict(b, date=f"2026-01-{i + start:02d}") for i, b in enumerate(bars)]


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestPatterns(unittest.TestCase):
    def test_doji(self):
        # 十字星: 实体极小
        bars = named([bar(10, 10.02, 10.5, 9.5)])
        out = run_expr(f"PS.scanPatterns({json.dumps(bars)})")
        # 单根无法判定趋势前置 → dir=0 形态仍输出
        names = [p["name"] for p in out]
        self.assertIn("十字星", names)

    def test_bullish_engulfing(self):
        # 下跌后: 前阴小实体, 今阳大实体包住
        bars = named([
            bar(12, 11.8, 12.1, 11.7),   # 下跌段
            bar(11.8, 11.5, 11.9, 11.4),
            bar(11.5, 11.2, 11.55, 11.1),
            bar(11.2, 10.9, 11.25, 10.8),
            bar(10.9, 10.7, 11.0, 10.6),
            bar(10.7, 10.5, 10.8, 10.4),  # 前阴: open 10.7 close 10.5
            bar(10.4, 11.1, 11.2, 10.3),  # 今阳: open 10.4 ≤ 前close, close 11.1 ≥ 前open
        ])
        out = run_expr(f"PS.scanPatterns({json.dumps(bars)})")
        names = [p["name"] for p in out]
        self.assertIn("看涨吞没", names)
        hit = next(p for p in out if p["name"] == "看涨吞没")
        self.assertEqual(hit["dir"], 1)
        self.assertEqual(hit["idx"], 6)

    def test_bearish_engulfing(self):
        bars = named([
            bar(10, 10.2, 10.4, 9.9),
            bar(10.2, 10.5, 10.6, 10.1),
            bar(10.5, 10.8, 10.9, 10.4),
            bar(10.8, 11.1, 11.2, 10.7),
            bar(11.1, 11.4, 11.5, 11.0),
            bar(11.4, 11.6, 11.7, 11.3),  # 前阳
            bar(11.7, 11.0, 11.8, 10.9),  # 今阴包阳
        ])
        out = run_expr(f"PS.scanPatterns({json.dumps(bars)})")
        names = [p["name"] for p in out]
        self.assertIn("看跌吞没", names)
        hit = next(p for p in out if p["name"] == "看跌吞没")
        self.assertEqual(hit["dir"], -1)

    def test_hammer_in_downtrend(self):
        # 下跌段后长下影小实体
        bars = named([
            bar(12, 11.8, 12.0, 11.7),
            bar(11.8, 11.6, 11.9, 11.5),
            bar(11.6, 11.3, 11.7, 11.2),
            bar(11.3, 11.0, 11.4, 10.9),
            bar(11.0, 10.8, 11.1, 10.7),
            bar(10.8, 10.6, 10.9, 10.5),
            bar(10.6, 10.55, 10.6, 10.0),  # 锤头: 实体 0.05, 下影 0.55, 上影 0
        ])
        out = run_expr(f"PS.scanPatterns({json.dumps(bars)})")
        names = [p["name"] for p in out]
        self.assertIn("锤头", names)

    def test_three_white_soldiers(self):
        bars = named([
            bar(10.0, 10.3, 10.4, 9.9),
            bar(10.3, 10.6, 10.7, 10.2),
            bar(10.6, 10.8, 10.9, 10.5),  # 前置震荡
            bar(10.7, 11.0, 11.1, 10.65),  # 三连阳起点
            bar(11.0, 11.3, 11.4, 10.95),
            bar(11.3, 11.6, 11.7, 11.25),
        ])
        out = run_expr(f"PS.scanPatterns({json.dumps(bars)})")
        names = [p["name"] for p in out]
        self.assertIn("红三兵", names)

    def test_patterns_at_exact_idx(self):
        bars = named([
            bar(10, 10.2, 10.4, 9.9),
            bar(10.2, 10.5, 10.6, 10.1),
            bar(10.5, 10.8, 10.9, 10.4),
            bar(10.8, 11.1, 11.2, 10.7),
            bar(11.1, 11.4, 11.5, 11.0),
            bar(11.4, 11.6, 11.7, 11.3),
            bar(11.7, 11.0, 11.8, 10.9),  # 看跌吞没 @ idx 6
        ])
        out = run_expr(f"""(() => {{
            const bars = {json.dumps(bars)};
            const all = PS.scanPatterns(bars);
            const hit = PS.patternsAt(bars, 6);
            const miss = PS.patternsAt(bars, 0);
            return {{ hitName: hit && hit.name, miss }};
        }})()""")
        self.assertEqual(out["hitName"], "看跌吞没")
        self.assertIsNone(out["miss"])

    def test_flat_series_no_patterns(self):
        bars = named([bar(10, 10, 10, 10)] * 10)
        out = run_expr(f"PS.scanPatterns({json.dumps(bars)})")
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
