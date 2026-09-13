# -*- coding: utf-8 -*-
"""主图线色防回归: BOLL 必须用自己的主题色, 不能和 MA10(amber) 撞色。

历史问题: BOLL 三条线曾直接复用 C().ma10, 与 MA10 同为 --chart-amber 黄色, 无法区分。

运行:
    venv/Scripts/python.exe -u visual/test/test_chart_colors.py
"""

import re
import sys
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

THEME_CSS = _VISUAL_DIR / "static" / "css" / "theme.css"
INDEX_HTML = _VISUAL_DIR / "static" / "index.html"

MAIN_LINE_TOKENS = ("--chart-ink", "--chart-amber", "--chart-rose", "--chart-boll")


def _theme_blocks():
    css = THEME_CSS.read_text(encoding="utf-8")
    parts = css.split('[data-theme="dark"]')
    if len(parts) != 2:
        raise AssertionError("theme.css 结构变化: 未找到 [data-theme=\"dark\"] 块")
    return {"light": parts[0], "dark": parts[1]}


def _var(block: str, name: str):
    m = re.search(re.escape(name) + r"\s*:\s*([^;]+);", block)
    return m.group(1).strip().lower() if m else None


class ChartColorTest(unittest.TestCase):
    def test_boll_token_defined_in_both_themes(self):
        for theme, block in _theme_blocks().items():
            self.assertIsNotNone(_var(block, "--chart-boll"), f"{theme} 缺少 --chart-boll")

    def test_main_chart_line_colors_are_distinct(self):
        for theme, block in _theme_blocks().items():
            colors = [_var(block, t) for t in MAIN_LINE_TOKENS]
            self.assertNotIn(None, colors, f"{theme} 缺少主图线色 token: {MAIN_LINE_TOKENS}")
            self.assertEqual(len(set(colors)), len(colors),
                             f"{theme} 主图线色有重复 (BOLL 不能与 MA 撞色): {dict(zip(MAIN_LINE_TOKENS, colors))}")

    def test_chart_var_map_has_boll(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertRegex(html, r"boll:\s*'--chart-boll'",
                         "CHART_VAR_MAP 未把 boll 映射到 --chart-boll")

    def test_boll_series_uses_own_color(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        start = html.index("const bNames = ['BOLL中轨'")
        block = html[start:start + 500]
        self.assertIn("C().boll", block, "BOLL 渲染未使用 C().boll")
        self.assertNotIn("C().ma10", block, "BOLL 渲染仍在复用 MA10 的颜色")


if __name__ == "__main__":
    unittest.main(verbosity=2)
