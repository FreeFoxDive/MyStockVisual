# -*- coding: utf-8 -*-
"""chips.js 重采样单元测试（Node）。"""

from __future__ import annotations

import json
import os
import shutil
import unittest

from js_test_util import run_node, require_node

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
VISUAL_DIR = os.path.dirname(TEST_DIR)
CHIPS_JS = os.path.join(VISUAL_DIR, "static", "js", "chips.js").replace("\\", "/")


def run_chips_js(body: str, *args: str) -> str:
    require_node()
    if not os.path.isfile(CHIPS_JS):
        raise unittest.SkipTest("chips.js missing")
    script = f"""
const ChipChart = require({json.dumps(CHIPS_JS)});
{body}
"""
    return run_node(script, *args)


def _chip(buckets):
    return {"buckets": buckets}


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestChipsResample(unittest.TestCase):
    BUCKETS = [
        {"price": 10.0, "weight": 0.0},
        {"price": 11.0, "weight": 1.0},
        {"price": 12.0, "weight": 0.0},
    ]

    def test_weight_at_endpoints_and_interp(self):
        payload = {"buckets": self.BUCKETS}
        out = json.loads(run_chips_js(
            "const p = JSON.parse(process.argv[1]);"
            "process.stdout.write(JSON.stringify(["
            "ChipChart.weightAt(p.buckets, 9.9),"
            "ChipChart.weightAt(p.buckets, 10.0),"
            "ChipChart.weightAt(p.buckets, 10.5),"
            "ChipChart.weightAt(p.buckets, 11.0),"
            "ChipChart.weightAt(p.buckets, 11.5),"
            "ChipChart.weightAt(p.buckets, 12.1)"
            "]));",
            json.dumps(payload),
        ))
        self.assertEqual(out[0], 0.0)
        self.assertEqual(out[1], 0.0)
        self.assertAlmostEqual(out[2], 0.5, places=6)
        self.assertEqual(out[3], 1.0)
        self.assertAlmostEqual(out[4], 0.5, places=6)
        self.assertEqual(out[5], 0.0)

    def test_resample_count_and_range(self):
        payload = {"chip": _chip(self.BUCKETS), "min": 10.0, "max": 12.0, "n": 4}
        out = json.loads(run_chips_js(
            "const p = JSON.parse(process.argv[1]);"
            "process.stdout.write(JSON.stringify(ChipChart.resample(p.chip, p.min, p.max, p.n)));",
            json.dumps(payload),
        ))
        self.assertEqual(len(out), 4)
        prices = [row[1] for row in out]
        self.assertEqual(prices, sorted(prices))
        self.assertGreater(prices[0], 10.0)
        self.assertLess(prices[-1], 12.0)
        self.assertAlmostEqual(out[0][0], 0.25, places=6)
        self.assertAlmostEqual(out[1][0], 0.75, places=6)
        self.assertAlmostEqual(out[2][0], 0.75, places=6)
        self.assertAlmostEqual(out[3][0], 0.25, places=6)

    def test_resample_outside_range_zero(self):
        payload = {"chip": _chip(self.BUCKETS), "min": 8.0, "max": 14.0, "n": 6}
        out = json.loads(run_chips_js(
            "const p = JSON.parse(process.argv[1]);"
            "process.stdout.write(JSON.stringify(ChipChart.resample(p.chip, p.min, p.max, p.n)));",
            json.dumps(payload),
        ))
        self.assertEqual(len(out), 6)
        self.assertEqual(out[0][0], 0.0)   # 8.5 在桶区间外
        self.assertEqual(out[-1][0], 0.0)  # 13.5 在桶区间外
        self.assertGreater(max(row[0] for row in out), 0.0)

    def test_bucket_count_clamped(self):
        out = json.loads(run_chips_js(
            "process.stdout.write(JSON.stringify(["
            "ChipChart.bucketCount(30), ChipChart.bucketCount(6000), ChipChart.bucketCount(600)"
            "]));"
        ))
        self.assertEqual(out[0], 60)    # 下限
        self.assertEqual(out[1], 900)   # 上限
        self.assertEqual(out[2], round(600 / 1.5))

    def test_resample_invalid_range(self):
        payload = {"chip": _chip(self.BUCKETS)}
        out = json.loads(run_chips_js(
            "const p = JSON.parse(process.argv[1]);"
            "process.stdout.write(JSON.stringify(ChipChart.resample(p.chip, 12.0, 10.0, 5)));",
            json.dumps(payload),
        ))
        self.assertEqual(out, [])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestChipsOverlay(unittest.TestCase):
    BUCKETS = TestChipsResample.BUCKETS
    COLORS = {
        "chipProfit": "#ef232a", "chipTrapped": "#14b143",
        "chipAvg": "#d4a017", "chipMedian": "#7e57c2",
    }

    def _overlay(self, chip):
        cfg = {
            "gi": 1, "left": "86%", "right": "3%", "top": "2%", "height": "80%",
            "colors": self.COLORS, "lastClose": 11.0,
            "min": 10.0, "max": 12.0, "pxHeight": 600,
        }
        return json.loads(run_chips_js(
            "const p = JSON.parse(process.argv[1]);"
            "const out = ChipChart.buildOverlay(p.chip, p.cfg);"
            "const pick = function(n){return out.series.filter(function(s){return s.name===n;})[0]||{};};"
            "process.stdout.write(JSON.stringify({"
            "names: out.series.map(function(s){return s.name;}),"
            "avgLabel: (pick('筹码均本').endLabel||{}).color,"
            "medianLabel: (pick('筹码中位价').endLabel||{}).color,"
            "medianLine: (pick('筹码中位价').lineStyle||{}).color"
            "}));",
            json.dumps({"chip": chip, "cfg": cfg}),
        ))

    def test_overlay_draws_both_cost_lines(self):
        chip = {"buckets": self.BUCKETS, "avgCost": 11.0, "medianCost": 10.8, "profitRatio": 0.5}
        out = self._overlay(chip)
        self.assertIn("筹码均本", out["names"])
        self.assertIn("筹码中位价", out["names"])
        # 数字标签颜色与对应线颜色一致
        self.assertEqual(out["medianLine"], self.COLORS["chipMedian"])
        self.assertEqual(out["medianLabel"], self.COLORS["chipMedian"])
        self.assertEqual(out["avgLabel"], self.COLORS["chipAvg"])

    def test_overlay_omits_median_when_absent(self):
        chip = {"buckets": self.BUCKETS, "avgCost": 11.0}
        out = self._overlay(chip)
        self.assertIn("筹码均本", out["names"])
        self.assertNotIn("筹码中位价", out["names"])
        self.assertEqual(out["avgLabel"], self.COLORS["chipAvg"])

    def test_line_color_matches_cost_lines(self):
        out = json.loads(run_chips_js(
            "const p = JSON.parse(process.argv[1]);"
            "const c = p.colors;"
            "process.stdout.write(JSON.stringify(["
            "ChipChart.lineColor('均本 11.00', c),"
            "ChipChart.lineColor('中位价 10.80', c),"
            "ChipChart.lineColor('获利 50.0%', c)"
            "]));",
            json.dumps({"colors": self.COLORS}),
        ))
        self.assertEqual(out[0], self.COLORS["chipAvg"])
        self.assertEqual(out[1], self.COLORS["chipMedian"])
        self.assertIsNone(out[2])

    def test_summary_lines_include_median(self):
        chip = {"buckets": self.BUCKETS, "avgCost": 11.0, "medianCost": 10.8, "profitRatio": 0.5}
        out = json.loads(run_chips_js(
            "const p = JSON.parse(process.argv[1]);"
            "process.stdout.write(JSON.stringify(ChipChart.summaryLines(p)));",
            json.dumps(chip),
        ))
        self.assertTrue(any("中位价" in line for line in out))
        self.assertTrue(any(line.startswith("均本") for line in out))

    def test_summary_lines_no_median_when_absent(self):
        chip = {"buckets": self.BUCKETS, "avgCost": 11.0, "profitRatio": 0.5}
        out = json.loads(run_chips_js(
            "const p = JSON.parse(process.argv[1]);"
            "process.stdout.write(JSON.stringify(ChipChart.summaryLines(p)));",
            json.dumps(chip),
        ))
        self.assertFalse(any("中位价" in line for line in out))


if __name__ == "__main__":
    unittest.main()

