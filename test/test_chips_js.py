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
class TestChipsSummaryFormat(unittest.TestCase):
    """摘要数字格式 + 两列结构化行。"""

    def test_fmt_num_caps_three_decimals_and_strips_zeros(self):
        out = json.loads(run_chips_js(
            "const p = JSON.parse(process.argv[1]);"
            "process.stdout.write(JSON.stringify(p.map(v => ChipChart.fmtNum(v))));",
            json.dumps([4.690, 3.4000, 2.0, 12345.6789, 1328.64, 0.5, None]),
        ))
        self.assertEqual(out, ["4.69", "3.4", "2", "12345.679", "1328.64", "0.5", "—"])

    def test_summary_rows_two_columns(self):
        chip = {"buckets": [], "avgCost": 4.69, "medianCost": 3.4,
                "profitRatio": 0.5, "pct90": [3.31, 3.63], "pct70": [3.34, 3.5],
                "source": "af", "period": "1w"}
        out = json.loads(run_chips_js(
            "const p = JSON.parse(process.argv[1]);"
            "process.stdout.write(JSON.stringify(ChipChart.summaryRows(p)));",
            json.dumps(chip),
        ))
        self.assertTrue(out[0]["title"])
        self.assertIn("周窗口", out[0]["label"])
        rows = {r["label"]: r for r in out[1:]}
        self.assertEqual(rows["获利"]["value"], "50.0%")
        self.assertEqual(rows["获利"]["color"], "chipProfit")
        self.assertEqual(rows["均本"]["value"], "4.69")
        self.assertEqual(rows["均本"]["color"], "chipAvg")
        self.assertEqual(rows["中位价"]["value"], "3.4")
        self.assertEqual(rows["90%"]["value"], "3.31~3.63")
        self.assertEqual(rows["70%"]["value"], "3.34~3.5")


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestChipsLabelOffsets(unittest.TestCase):
    """均本/中位价 endLabel 避让偏移 (价格接近时上下错开)。"""

    def _offsets(self, avg, median, lo, hi, px):
        return json.loads(run_chips_js(
            "const p = JSON.parse(process.argv[1]);"
            "process.stdout.write(JSON.stringify("
            "ChipChart.endLabelOffsets(p.a, p.m, p.lo, p.hi, p.px)));",
            json.dumps({"a": avg, "m": median, "lo": lo, "hi": hi, "px": px}),
        ))

    def test_equal_prices_spread_symmetrically(self):
        out = self._offsets(11.0, 11.0, 10.0, 12.0, 600)
        self.assertEqual(out["avg"], [2, -7])      # 每侧最大位移
        self.assertEqual(out["median"], [2, 7])

    def test_null_median_keeps_base_offset(self):
        out = self._offsets(11.0, None, 10.0, 12.0, 600)
        self.assertEqual(out["avg"], [2, 0])
        self.assertEqual(out["median"], [2, 0])

    def test_invalid_range_keeps_base_offset(self):
        out = self._offsets(11.0, 11.0, 12.0, 10.0, 600)   # 区间反向
        self.assertEqual(out["avg"], [2, 0])
        out = self._offsets(11.0, 11.0, 10.0, 12.0, 0)     # 高度为 0
        self.assertEqual(out["median"], [2, 0])


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestChipsCostEndLabel(unittest.TestCase):
    """成本线末端标签构造。"""

    def test_color_weight_and_offset(self):
        out = json.loads(run_chips_js(
            "const l = ChipChart.costEndLabel('#123456', 4.69, [2, -3]);"
            "process.stdout.write(JSON.stringify({"
            "color: l.color, fs: l.fontSize, fw: l.fontWeight,"
            "offset: l.offset, text: l.formatter()"
            "}));"
        ))
        self.assertEqual(out["color"], "#123456")
        self.assertEqual(out["fs"], 11)
        self.assertEqual(out["fw"], "bold")
        self.assertEqual(out["offset"], [2, -3])
        self.assertEqual(out["text"], "4.69")

    def test_offset_defaults_to_base(self):
        out = json.loads(run_chips_js(
            "process.stdout.write(JSON.stringify("
            "ChipChart.costEndLabel('#111111', 1.0).offset));"
        ))
        self.assertEqual(out, [2, 0])


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
            "medianLine: (pick('筹码中位价').lineStyle||{}).color,"
            "avgOffset: (pick('筹码均本').endLabel||{}).offset,"
            "medianOffset: (pick('筹码中位价').endLabel||{}).offset"
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
        self.assertEqual(out["avgOffset"], [2, 0])   # 无中位价 → 不避让

    def test_end_labels_spread_when_prices_close(self):
        # 11.0 vs 10.97, 区间 10~12 高 600px → 像素间距 9px < 14px → 上下错开
        chip = {"buckets": self.BUCKETS, "avgCost": 11.0, "medianCost": 10.97, "profitRatio": 0.5}
        out = self._overlay(chip)
        self.assertLess(out["avgOffset"][1], 0)      # 高价(均本)在上
        self.assertGreater(out["medianOffset"][1], 0)
        self.assertEqual(out["avgOffset"][0], 2)     # x 偏移不变
        # 原有 9px 间距 + 两侧位移 → 标签中心间距 ≥ 阈值 14px
        gap_px = abs(11.0 - 10.97) * 600 / (12.0 - 10.0)
        self.assertGreaterEqual(gap_px + out["medianOffset"][1] - out["avgOffset"][1], 14)

    def test_end_labels_direction_follows_price(self):
        # 均本低于中位价 → 均本下移、中位价上移
        chip = {"buckets": self.BUCKETS, "avgCost": 10.9, "medianCost": 10.93, "profitRatio": 0.5}
        out = self._overlay(chip)
        self.assertGreater(out["avgOffset"][1], 0)
        self.assertLess(out["medianOffset"][1], 0)

    def test_end_labels_flat_when_prices_far(self):
        # 11.0 vs 10.8 → 像素间距 60px, 无需错开
        chip = {"buckets": self.BUCKETS, "avgCost": 11.0, "medianCost": 10.8, "profitRatio": 0.5}
        out = self._overlay(chip)
        self.assertEqual(out["avgOffset"], [2, 0])
        self.assertEqual(out["medianOffset"], [2, 0])

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

