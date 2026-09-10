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


if __name__ == "__main__":
    unittest.main()
