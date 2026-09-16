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

    def test_strong_text_is_black_light_white_dark(self):
        """刻度/图例/读数用的强对比文本: 亮色黑、暗色白 (原来两主题都是中灰, 看着暗淡)。"""
        for theme, block in _theme_blocks().items():
            want = "#000000" if theme == "light" else "#ffffff"
            self.assertEqual(_var(block, "--chart-text"), want,
                             f"{theme} 的 --chart-text 应为 {want} (图表刻度/图例)")
            self.assertEqual(_var(block, "--strong-text"), want,
                             f"{theme} 的 --strong-text 应为 {want} (顶部栏读数)")
            self.assertEqual(_var(block, "--strong-text"), _var(block, "--chart-text"),
                             f"{theme} 两套「强对比文本」取值必须一致")

    def test_legend_segments_use_series_colors(self):
        """图例每段必须用自己的线色 (DIF 段用 macdDif 等), 不能整行一个颜色。"""
        html = INDEX_HTML.read_text(encoding="utf-8")
        legend = html[html.index("function paintLegend"):html.index("function _renderDrawingsNow")]
        self.assertIn("putSegs", legend, "图例应走分段着色")
        self.assertNotIn("put(0, parts.join", legend, "旧的整行单色图例应已移除")
        self.assertIn("C().boll", legend, "BOLL 段用线色")
        self.assertIn("maFills[i]", legend, "MA 段用均线色")
        self.assertIn("C().atr", legend, "ATR 段用线色")
        # 段色 ↔ 各 series 的 lineStyle.color 成对出现 (防图例色与线色漂移)
        pairs = [("DIF", "macdDif"), ("DEA", "macdDea"), ("K ", "kdjK"), ("D ", "kdjD"),
                 ("J ", "kdjJ"), ("RSI1", "rsi6"), ("RSI2", "rsi12"), ("RSI3", "rsi24"),
                 ("MAOBV", "maobv"), ("WR14", "rsi6"), ("CCI14", "macdDif"),
                 ("BIAS6", "ma5"), ("BIAS12", "ma10"), ("BIAS24", "ma20"),
                 ("+DI", "up"), ("-DI", "down"), ("ADX", "macdDea")]
        for label, token in pairs:
            pat = (r"text: `" + re.escape(label) + r"[^`]*`,\s*fill: [^,\n]*" + re.escape(token))
            self.assertRegex(legend, pat, f"图例 {label.strip()} 段应使用 {token} (与线同色)")

    def test_ma_line_colors_shared(self):
        """均线配色只有一处来源 (maLineColors), 线/tooltip/图例都读它。"""
        html = INDEX_HTML.read_text(encoding="utf-8")
        fn = html[html.index("function maLineColors"):]
        fn = fn[:fn.index("}")]
        for token in ("C().ma5", "C().ma10", "C().ma20"):
            self.assertIn(token, fn, f"maLineColors 缺少 {token}")
        self.assertIn("const maColors = maLineColors();", html, "updateChart 应改用共享取色")
        self.assertNotIn("const maColors = [C().ma5", html, "旧的内联取色应已移除")


if __name__ == "__main__":
    unittest.main(verbosity=2)
