# -*- coding: utf-8 -*-
"""画线设置条: 随视口缩放的样式, 以及落到线条外侧的落点。

placeDrawChrome / drawChromeBounds / drawAvoidRect 从 index.html 抽出, 用 Node 跑。
水平线的避开矩形走真实 drawings.js 几何, 免得手写的带和渲染对不上。

运行:
    venv/Scripts/python.exe -u visual/test/test_draw_menu_place_js.py
"""

import json
import re
import unittest
from pathlib import Path

from js_test_util import require_node, run_node

_VISUAL_DIR = Path(__file__).resolve().parents[1]
INDEX_HTML = _VISUAL_DIR / "static" / "index.html"
DRAWINGS_JS = _VISUAL_DIR / "static" / "js" / "drawings.js"


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise AssertionError(f"index.html 中找不到 function {name}")
    start = src.index("{", m.end() - 1)
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():i + 1]
    raise AssertionError(f"function {name} 大括号不配对")


def _contains(pos, size, x, y):
    return (pos["left"] < x < pos["left"] + size["w"]
            and pos["top"] < y < pos["top"] + size["h"])


_FNS = [
    "clampDrawChrome", "drawChromeHits", "placeDrawChrome", "drawChromeBounds",
    "_chromePts", "drawAvoidRect",
]

_SCRIPT = r"""
globalThis.Drawings = require(process.argv[1]);
const menu = { w: 200, h: 48 };
const chart = { left: 4, top: 4, right: 896, bottom: 596 };
const upper = placeDrawChrome(menu, { x: 90, y: 70, w: 220, h: 80 }, chart);
const lower = placeDrawChrome(menu, { x: 90, y: 530, w: 220, h: 40 }, chart);
const wideBounds = { left: 60, top: 4, right: 400, bottom: 596 };
const wide = placeDrawChrome({ w: 500, h: 48 }, { x: 80, y: 40, w: 120, h: 40 }, wideBounds);
const hlineAvoid = { x: 40, y: 190, w: 700, h: 20 };
const hlineMenu = { w: 220, h: 40 };
const hline = placeDrawChrome(hlineMenu, hlineAvoid, chart);
const P = {
  grid: { left: 40, right: 800, top: 10, bottom: 400 },
  x: (idx) => 100 + idx * 20,
  y: (p) => 400 - p * 10,
};
const ctx = { n: 10, dateMap: { map: {}, dates: [] }, bars: [] };
const hlineDrawing = drawAvoidRect(
  { type: 'hline', points: [{ t: null, p: 10, off: 0 }], extendRight: false }, P, ctx);
const trend = drawAvoidRect({
  type: 'trend', extendRight: true,
  points: [{ t: null, p: 10, off: -4 }, { t: null, p: 14, off: -1 }],
}, P, ctx);
const beside = { w: 220, h: 80 };
const prefer = clampDrawChrome({ left: 100, top: 180 }, beside, chart);
const monitor = drawChromeHits(prefer, beside, hlineAvoid)
  ? placeDrawChrome(beside, hlineAvoid, chart) : prefer;
const wrap = {
  clientWidth: 900, clientHeight: 600,
  getBoundingClientRect: () => ({ left: 0, top: 0, right: 900, bottom: 600, width: 900, height: 600 }),
};
const els = {
  'draw-palette': {
    style: { display: 'flex' }, offsetWidth: 44, offsetHeight: 300,
    getBoundingClientRect: () => ({ left: 212, top: 8, right: 256, bottom: 308, width: 44, height: 300 }),
  },
  'side-panel': {
    style: { display: 'flex' }, offsetWidth: 204, offsetHeight: 600,
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 204, bottom: 600, width: 204, height: 600 }),
  },
};
globalThis.document = { getElementById: (id) => els[id] || null };
const bounds = drawChromeBounds(wrap);
els['draw-palette'].offsetWidth = 0;
const sideOnly = drawChromeBounds(wrap);
process.stdout.write(JSON.stringify({
  upper, lower, wide, hline, hlineDrawing, trend, monitor, preferHits: drawChromeHits(prefer, beside, hlineAvoid),
  bounds, sideOnly,
}));
"""


class DrawMenuPlaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_node()
        cls.src = INDEX_HTML.read_text(encoding="utf-8")
        script = "\n".join(_extract_fn(cls.src, name) for name in _FNS) + "\n" + _SCRIPT
        cls.out = json.loads(run_node(script, str(DRAWINGS_JS)))

    def test_upper_line_menu_sits_below_without_covering_handles(self):
        pos = self.out["upper"]
        self.assertGreaterEqual(pos["top"], 150, "上半区空位在线的下方")
        for x, y in ((100, 80), (300, 140)):
            self.assertFalse(_contains(pos, {"w": 200, "h": 48}, x, y), f"端点 ({x},{y}) 被菜单盖住")

    def test_lower_line_menu_flips_above(self):
        pos = self.out["lower"]
        self.assertLessEqual(pos["top"] + 48, 530, "靠底时菜单要翻到线的上方")
        for x, y in ((100, 540), (300, 560)):
            self.assertFalse(_contains(pos, {"w": 200, "h": 48}, x, y))

    def test_overwide_menu_stays_inside_and_clears_toolbar(self):
        pos = self.out["wide"]
        self.assertEqual(pos["left"], 60, "左边贴住工具条让出的起点, 不再钻到工具条下面")
        self.assertGreaterEqual(pos["top"], 4)
        self.assertLessEqual(pos["top"] + 48, 596)

    def test_hline_menu_does_not_contain_line_y(self):
        pos = self.out["hline"]
        self.assertFalse(_contains(pos, {"w": 220, "h": 40}, 400, 200))
        self.assertTrue(pos["top"] >= 210 or pos["top"] + 40 <= 190)

    def test_hline_avoid_is_a_full_grid_band(self):
        band = self.out["hlineDrawing"]
        self.assertEqual(band["x"], 40)
        self.assertEqual(band["w"], 760)
        self.assertLess(band["y"], 300)          # P.y(10) = 300, 带要包住这条 y
        self.assertGreater(band["y"] + band["h"], 300)

    def test_extended_trend_avoid_reaches_grid_right(self):
        box = self.out["trend"]
        self.assertGreaterEqual(box["x"] + box["w"], 800)

    def test_monitor_moves_off_the_line_when_it_would_cover_it(self):
        self.assertTrue(self.out["preferHits"], "前置: 挨着菜单的那一格确实压住了水平线")
        pos = self.out["monitor"]
        self.assertTrue(pos["top"] >= 210 or pos["top"] + 80 <= 190)

    def test_bounds_clear_palette_and_side_panel(self):
        self.assertEqual(self.out["bounds"]["left"], 260, "工具条比侧栏更靠右, 以工具条右缘为准")
        self.assertEqual(self.out["sideOnly"]["left"], 208, "工具条收起后只让开侧栏")


class DrawMenuPlaceStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def test_coarse_targets_scale_and_palette_stays_44(self):
        self.assertIn("min-width: clamp(28px, 2.2vw, 44px);", self.src)
        self.assertIn("min-height: clamp(28px, 2.2vw, 44px);", self.src)
        self.assertIn("#draw-menu button:not(.dm-dot)", self.src)
        self.assertIn("width: clamp(18px, 1.6vw, 32px);", self.src)
        self.assertIn("min-width: 0; min-height: 0;", self.src)
        self.assertIn("#draw-palette .dp-btn { width: 44px; height: 44px; flex-shrink: 0; }", self.src)
        self.assertNotIn("#draw-menu button { min-width: 44px; min-height: 44px; }", self.src)
        self.assertIn("width: clamp(160px, 16vw, 250px);", self.src)
        self.assertIn("width: clamp(108px, 10vw, 150px);", self.src)

    def test_chrome_is_placed_outside_the_stroke(self):
        menu = _extract_fn(self.src, "positionDrawMenu")
        self.assertIn("if (!d) { hideDrawMenu(); return; }", menu)
        self.assertIn("placeDrawChrome(", menu)
        self.assertIn("drawAvoidRect(", menu)
        self.assertIn("placeDrawChrome(", _extract_fn(self.src, "positionSuggestBox"))
        self.assertIn("placeDrawChrome(", _extract_fn(self.src, "positionPriceInput"))
        monitor = _extract_fn(self.src, "openMonitorInput")
        self.assertLess(monitor.index("clampDrawChrome("), monitor.index("placeDrawChrome("),
                        "监控框先试着挨着菜单, 压住线才改到外侧")
        render = _extract_fn(self.src, "_renderDrawingsNow")
        self.assertLess(render.index("positionDrawMenu();"), render.index("positionSuggestBox();"))
        self.assertLess(render.index("positionSuggestBox();"), render.index("positionPriceInput();"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
