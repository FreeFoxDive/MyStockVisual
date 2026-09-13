# -*- coding: utf-8 -*-
"""图表标注回归测试: 未来预测区色带 + 零轴 markLine 标签。

两个曾经的实际观感 bug:
  1. 画线模式下右侧预留的 8 根未来 bar 没有分类标签, 呈一条空白带, 被当成渲染缺口
     (日期「顶不到右边缘」)。修法: 该区铺淡色带 + 「预测区」标注 (buildFutureZoneMarkArea)。
  2. MACD/溢价/OBV/BIAS 的零轴 markLine 未写 label, ECharts 默认在零轴末端渲染数值 0;
     单边行情下零轴贴近面板底部, 那个 0 落在日期刻度区像横轴出了异常刻度。修法: label show:false。

从 index.html 抽取 buildFutureZoneMarkArea **真实源码**执行 (抽不到即失败), 并静态守住两处零轴。

运行:
    venv/Scripts/python.exe -u visual/test/test_chart_annotations_js.py
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
INDEX_HTML = _VISUAL_DIR / "static" / "index.html"


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
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


class ZeroLineLabelTest(unittest.TestCase):
    """零轴 markLine 必须显式关闭默认数值标签。"""

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def test_kline_zero_line_hides_default_label(self):
        # K线指标面板: macd / premium / obv / bias 共用的零轴分支
        seg = self.src[self.src.index("p === 'macd' || p === 'premium'")::]
        seg = seg[:seg.index("data: [{ yAxis: 0 }]")]
        self.assertIn("label: { show: false }", seg,
                      "零轴 markLine 不写 label 时 ECharts 默认显示数值 0")
        self.assertIn("C().zeroLine", seg)

    def test_intraday_zero_line_hides_default_label(self):
        # 分时里 MACD 零轴是另一处独立赋值
        m = re.search(r"s\.markLine = \{[^}]*zeroLine[^;]*\};", self.src)
        self.assertIsNotNone(m, "找不到分时 MACD 零轴 markLine")
        self.assertIn("label: { show: false }", m.group(0))

    def test_rsi_reference_labels_still_shown(self):
        """RSI/KDJ 等参考线标注是有意为之, 不能被一并改掉。"""
        self.assertIn("label: { show: true, formatter: '70'", self.src)
        self.assertIn("label: { show: true, formatter: '80'", self.src)


class FutureZoneStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def test_update_chart_attaches_zone_per_grid(self):
        self.assertIn("buildFutureZoneMarkArea(dates, futureBars, C())", self.src)
        self.assertIn("zoneMarked.has(gi)", self.src, "每个 grid 只应挂一次色带")
        self.assertIn("gi === chipGi", self.src, "筹码 value 轴不得标注")

    def test_zone_marks_are_silent(self):
        # 色带不能抢 axis tooltip (与缺口 markArea 一致)
        idx = self.src.index("buildFutureZoneMarkArea(dates, futureBars, C())")
        self.assertIn("s.markArea.silent", self.src[idx:idx + 600])


class FutureZoneThemeTest(unittest.TestCase):
    """色带颜色必须有主题 token: C() 读不到变量会返回空串, 色带会静默不可见。"""

    PREDICT_VARS = ("--chart-predict-fill", "--chart-predict-border")

    @classmethod
    def setUpClass(cls):
        css = (_VISUAL_DIR / "static" / "css" / "theme.css").read_text(encoding="utf-8")
        parts = css.split('[data-theme="dark"]')
        if len(parts) != 2:
            raise AssertionError('theme.css 结构变化: 未找到 [data-theme="dark"] 块')
        cls.themes = {"light": parts[0], "dark": parts[1]}
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def test_predict_tokens_defined_in_both_themes(self):
        for theme, block in self.themes.items():
            for var in self.PREDICT_VARS:
                self.assertRegex(block, re.escape(var) + r"\s*:\s*[^;]+;",
                                 f"{theme} 主题缺少 {var}")

    def test_chart_var_map_maps_predict_tokens(self):
        self.assertRegex(self.src, r"predictFill:\s*'--chart-predict-fill'")
        self.assertRegex(self.src, r"predictBorder:\s*'--chart-predict-border'")


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
class FutureZoneBehaviorTest(unittest.TestCase):
    COLORS = {"predictFill": "rgba(91,143,249,0.08)",
              "predictBorder": "rgba(91,143,249,0.35)",
              "text": "#787b86"}

    @classmethod
    def setUpClass(cls):
        cls.fn = _extract_fn(INDEX_HTML.read_text(encoding="utf-8"), "buildFutureZoneMarkArea")

    def _run(self, dates, future_bars):
        script = (
            self.fn + "\n"
            + "const c = JSON.parse(process.argv[1]);"
            + "process.stdout.write(JSON.stringify(buildFutureZoneMarkArea(c.dates, c.futureBars, c.colors)));"
        )
        payload = {"dates": dates, "futureBars": future_bars, "colors": self.COLORS}
        proc = subprocess.run(["node", "-e", script, json.dumps(payload)],
                              capture_output=True, check=True)
        return json.loads(proc.stdout.decode("utf-8"))

    def test_returns_band_when_future_bars_exist(self):
        out = self._run(["D%d" % i for i in range(500)], 8)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["xAxis"], 499.5, "色带左缘 = 最后一根 bar 的右边界")
        self.assertEqual(out[1]["xAxis"], 507.5, "色带右缘 = 最后一个未来槽的右边界")
        self.assertEqual(out[0]["label"]["formatter"], "预测区")
        self.assertEqual(out[0]["itemStyle"]["color"], self.COLORS["predictFill"])
        self.assertEqual(out[0]["itemStyle"]["borderColor"], self.COLORS["predictBorder"])
        # 第二项是收尾锚点, 不带样式/标签
        self.assertNotIn("label", out[1])

    def test_none_when_no_future_bars(self):
        self.assertIsNone(self._run(["D%d" % i for i in range(10)], 0))

    def test_none_without_dates(self):
        self.assertIsNone(self._run([], 8))

    def test_band_extends_with_future_bars(self):
        out = self._run(["D%d" % i for i in range(20)], 3)
        self.assertEqual(out[0]["xAxis"], 19.5)
        self.assertEqual(out[1]["xAxis"], 22.5)


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
class FutureZoneWiringTest(unittest.TestCase):
    """执行 updateChart 里真实的挂载片段, 确认每个 grid 只挂一条且不碰筹码轴。"""

    @classmethod
    def setUpClass(cls):
        src = INDEX_HTML.read_text(encoding="utf-8")
        cls.fn = _extract_fn(src, "buildFutureZoneMarkArea")
        m = re.search(r"  const zoneItem = buildFutureZoneMarkArea\([\s\S]*?\n  \}\n", src)
        if not m:
            raise AssertionError("index.html 中找不到未来预测区挂载片段")
        cls.block = m.group(0)

    def _run(self):
        series = [
            {"name": "K线", "xAxisIndex": 0, "markArea": {"silent": True, "z": 1, "data": [["缺口"]]}},
            {"name": "MA5", "xAxisIndex": 0},
            {"name": "VOL", "xAxisIndex": 1},
            {"name": "DIF", "xAxisIndex": 2},
            {"name": "筹码", "xAxisIndex": 3},
        ]
        script = (
            self.fn + "\n"
            + "const colors = JSON.parse(process.argv[1]);"
            + "const C = () => colors;"
            + "const dates = Array.from({ length: 500 }, (_, i) => 'D' + i);"
            + "const futureBars = 8;"
            + "const chipGi = 3;"
            + "let series = JSON.parse(process.argv[2]);"
            + self.block
            + "process.stdout.write(JSON.stringify(series));"
        )
        payload = json.dumps(series)
        colors = json.dumps({"predictFill": "F", "predictBorder": "B", "text": "T"})
        proc = subprocess.run(["node", "-e", script, colors, payload],
                              capture_output=True, check=True)
        return json.loads(proc.stdout.decode("utf-8"))

    def test_one_band_per_chart_grid_and_chip_untouched(self):
        out = self._run()
        k, ma5, vol, dif, chip = out
        # K线已有缺口 markArea: 色带 concat 进去, 原缺口保留
        self.assertEqual(len(k["markArea"]["data"]), 2)
        self.assertEqual(k["markArea"]["data"][0], ["缺口"])
        # 同一 grid 的第二个 series 不再重复挂
        self.assertNotIn("markArea", ma5)
        self.assertIn("markArea", vol)
        self.assertIn("markArea", dif)
        self.assertTrue(vol["markArea"]["silent"], "色带不得抢 axis tooltip")
        self.assertEqual(vol["markArea"]["z"], 1)
        # 筹码 value 轴完全不动
        self.assertNotIn("markArea", chip)

    def test_band_item_has_label_and_band_edges(self):
        out = self._run()
        band = out[2]["markArea"]["data"][0]
        self.assertEqual(band[0]["label"]["formatter"], "预测区")
        self.assertEqual(band[0]["xAxis"], 499.5)
        self.assertEqual(band[1]["xAxis"], 507.5)


class MarkPointTooltipTest(unittest.TestCase):
    """K线 markPoint 提示框: 形态/金叉死叉点只有 name 没有 value, 缺项必须跳过。

    回归: 形态倒三角悬浮显示 "943 undefined" —— 943 是 coord 横坐标(该 bar 下标),
    undefined 是缺失的 value, 由 buildTradeMarkers 里无条件拼接的 formatter 渲染出来。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _marks_block(self):
        """updateChart 里构建 金叉死叉/形态 extraMarks 的那段真实源码。"""
        start = self.src.index("金叉死叉 + K线形态 markPoint")
        end = self.src.index("const klineMarkPoint", start)
        return self.src[start:end]

    def test_formatter_skips_missing_fields(self):
        self.assertIn("[p.name, p.value].filter(Boolean).join('<br/>')", self.src)
        self.assertNotIn("p.name + '<br/>' + p.value", self.src,
                         "无条件拼接会把缺失的 value 渲染成 undefined")

    def test_tooltip_attached_even_without_trades(self):
        # 否则没有交易记录的标的, 形态三角悬浮完全无反应
        self.assertIn("return { symbol: 'none', tooltip: KLINE_MARKPOINT_TOOLTIP }", self.src)
        self.assertIn("return { tooltip: KLINE_MARKPOINT_TOOLTIP, data: points }", self.src)

    def test_pattern_items_carry_name_value_and_hide_label(self):
        block = self._marks_block()
        self.assertIn("name: pt.name", block, "形态标记缺 name, 提示框会渲染 undefined")
        self.assertIn("value: pt.note", block)
        self.assertIn("label: { show: false }", block,
                      "形态标记不配 label 时, ECharts 默认标签会把坐标/undefined 画到图上")

    def test_cross_items_carry_name(self):
        block = self._marks_block()
        self.assertIn("name: '金叉'", block)
        self.assertIn("name: '死叉'", block)


if __name__ == "__main__":
    unittest.main(verbosity=2)
