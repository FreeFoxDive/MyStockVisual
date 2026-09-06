# -*- coding: utf-8 -*-
"""gaps.js 单元测试（Node）。"""

from __future__ import annotations

import json
import os
import shutil
import unittest

from js_test_util import run_node, require_node

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
VISUAL_DIR = os.path.dirname(TEST_DIR)
GAPS_JS = os.path.join(VISUAL_DIR, "static", "js", "gaps.js").replace("\\", "/")


def run_gaps_js(body: str, *args: str) -> str:
    require_node()
    real = os.path.join(VISUAL_DIR, "static", "js", "gaps.js")
    if not os.path.isfile(real):
        raise unittest.SkipTest("gaps.js missing")
    script = f"""
const GapScanner = require({json.dumps(GAPS_JS)});
{body}
"""
    return run_node(script, *args)


def scan(klines, opts=None):
    payload = {"klines": klines, "opts": opts or {}}
    out = run_gaps_js(
        "const p = JSON.parse(process.argv[1]);"
        "process.stdout.write(JSON.stringify(GapScanner.scanGaps(p.klines, p.opts)));",
        json.dumps(payload),
    )
    return json.loads(out)


def bar(date, o, h, l, c, volume=1_000_000, **kw):
    d = {
        "date": date,
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "volume": volume,
    }
    d.update(kw)
    return d


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestGapsJs(unittest.TestCase):
    def test_gap_up_detected(self):
        klines = [
            bar("2024-01-01", 10, 11, 9.5, 10.5),
            bar("2024-01-02", 12, 13, 12, 12.5),  # low 12 > prev high 11
        ]
        gaps = scan(klines)
        self.assertEqual(len(gaps), 1)
        g = gaps[0]
        self.assertEqual(g["dir"], "up")
        self.assertAlmostEqual(g["bottom"], 11.0)
        self.assertAlmostEqual(g["top"], 12.0)
        self.assertEqual(g["startIdx"], 1)
        self.assertEqual(g["endIdx"], 1)
        self.assertAlmostEqual(g["spread"], 1.0)

    def test_gap_down_detected(self):
        klines = [
            bar("2024-01-01", 10, 11, 9.5, 10.5),
            bar("2024-01-02", 8, 8.5, 7.5, 8),  # high 8.5 < prev low 9.5
        ]
        gaps = scan(klines)
        self.assertEqual(len(gaps), 1)
        g = gaps[0]
        self.assertEqual(g["dir"], "down")
        self.assertAlmostEqual(g["top"], 9.5)
        self.assertAlmostEqual(g["bottom"], 8.5)

    def test_fully_filled_hidden(self):
        klines = [
            bar("2024-01-01", 10, 11, 9.5, 10.5),
            bar("2024-01-02", 12, 13, 12, 12.5),  # up gap 11~12
            bar("2024-01-03", 11.5, 12, 10.5, 11),  # low 10.5 <= 11 → filled
        ]
        gaps = scan(klines)
        self.assertEqual(gaps, [])

    def test_partial_fill_shrinks_up_gap(self):
        klines = [
            bar("2024-01-01", 10, 11, 9.5, 10.5),
            bar("2024-01-02", 12, 13, 12, 12.5),  # gap 11~12
            bar("2024-01-03", 11.7, 12.2, 11.4, 11.8),  # low 11.4 shrinks top
        ]
        gaps = scan(klines)
        self.assertEqual(len(gaps), 1)
        g = gaps[0]
        self.assertEqual(g["dir"], "up")
        self.assertAlmostEqual(g["bottom"], 11.0)
        self.assertAlmostEqual(g["top"], 11.4)
        self.assertAlmostEqual(g["top0"], 12.0)
        self.assertAlmostEqual(g["bottom0"], 11.0)
        self.assertEqual(g["endIdx"], 2)

    def test_partial_fill_shrinks_down_gap(self):
        klines = [
            bar("2024-01-01", 10, 11, 9.5, 10.5),
            bar("2024-01-02", 8, 8.5, 7.5, 8),  # gap 8.5~9.5
            bar("2024-01-03", 8.8, 9.0, 8.2, 8.9),  # high 9.0 raises bottom
        ]
        gaps = scan(klines)
        self.assertEqual(len(gaps), 1)
        g = gaps[0]
        self.assertEqual(g["dir"], "down")
        self.assertAlmostEqual(g["top"], 9.5)
        self.assertAlmostEqual(g["bottom"], 9.0)

    def test_halt_bar_skips_gap(self):
        klines = [
            bar("2024-01-01", 10, 11, 9.5, 10.5),
            bar("2024-01-02", 12, 13, 12, 12.5, volume=0),  # halt
            bar("2024-01-03", 14, 15, 14, 14.5),
        ]
        gaps = scan(klines)
        self.assertEqual(gaps, [])

    def test_halted_flag_skips(self):
        klines = [
            bar("2024-01-01", 10, 11, 9.5, 10.5),
            bar("2024-01-02", 12, 13, 12, 12.5, halted=True),
        ]
        gaps = scan(klines)
        self.assertEqual(gaps, [])

    def test_max_visible_default_is_two(self):
        out = json.loads(run_gaps_js(
            "process.stdout.write(JSON.stringify(GapScanner.MAX_VISIBLE));"
        ))
        self.assertEqual(out, 2)

    def test_max_visible_truncates_oldest(self):
        klines = [bar("2024-01-01", 10, 10.5, 9.5, 10)]
        # create 5 unfilled up gaps by stepping higher each day
        price = 10.5
        for i in range(5):
            lo = price + 1
            hi = lo + 1
            klines.append(bar(f"2024-01-{i+2:02d}", lo, hi, lo, lo + 0.5))
            price = hi
        gaps = scan(klines)  # default MAX_VISIBLE=2
        self.assertEqual(len(gaps), 2)
        # newest two startIdx should be the last two gap bars
        self.assertEqual(gaps[0]["startIdx"], 4)
        self.assertEqual(gaps[1]["startIdx"], 5)

    def test_find_gap_at_by_price(self):
        klines = [
            bar("2024-01-01", 10, 11, 9.5, 10.5),
            bar("2024-01-02", 12, 13, 12, 12.5),  # gap 11~12
            bar("2024-01-03", 12.2, 12.8, 12.1, 12.5),
        ]
        payload = {"klines": klines}
        out = json.loads(run_gaps_js(
            "const p = JSON.parse(process.argv[1]);"
            "const gaps = GapScanner.scanGaps(p.klines);"
            "process.stdout.write(JSON.stringify({"
            "hit: GapScanner.findGapAt(gaps, 2, 11.5),"
            "miss: GapScanner.findGapAt(gaps, 2, 13.0),"
            "early: GapScanner.findGapAt(gaps, 0, 11.5)"
            "}));",
            json.dumps(payload),
        ))
        self.assertIsNotNone(out["hit"])
        self.assertEqual(out["hit"]["dir"], "up")
        self.assertIsNone(out["miss"])
        self.assertIsNone(out["early"])

    def test_is_gap_period(self):
        out = json.loads(run_gaps_js(
            "process.stdout.write(JSON.stringify({"
            "d: GapScanner.isGapPeriod('1d'),"
            "m60: GapScanner.isGapPeriod('60m'),"
            "m5: GapScanner.isGapPeriod('5m'),"
            "periods: GapScanner.GAP_PERIODS"
            "}));"
        ))
        self.assertTrue(out["d"])
        self.assertTrue(out["m60"])
        self.assertFalse(out["m5"])
        self.assertEqual(out["periods"], ["60m", "1d", "1w", "1M"])

    def test_build_mark_area(self):
        klines = [
            bar("2024-01-01", 10, 11, 9.5, 10.5),
            bar("2024-01-02", 12, 13, 12, 12.5),
        ]
        dates = [k["date"] for k in klines]
        payload = {
            "klines": klines,
            "dates": dates,
            "colors": {"gapFill": "rgba(1,2,3,0.2)", "gapBorder": "rgba(4,5,6,0.4)"},
        }
        out = json.loads(run_gaps_js(
            "const p = JSON.parse(process.argv[1]);"
            "const gaps = GapScanner.scanGaps(p.klines);"
            "process.stdout.write(JSON.stringify(GapScanner.buildMarkArea(gaps, p.dates, p.colors)));",
            json.dumps(payload),
        ))
        self.assertIsNotNone(out)
        self.assertEqual(len(out["data"]), 1)
        self.assertEqual(out["data"][0][0]["coord"][0], "2024-01-02")
        self.assertAlmostEqual(out["data"][0][0]["coord"][1], 12.0)
        self.assertAlmostEqual(out["data"][0][1]["coord"][1], 11.0)
        self.assertEqual(out["itemStyle"]["color"], "rgba(1,2,3,0.2)")


if __name__ == "__main__":
    unittest.main()
