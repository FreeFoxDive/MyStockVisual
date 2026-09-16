# -*- coding: utf-8 -*-
"""画线模式与筹码峰共存回归测试。

曾经的 bug: 进入画线模式后右侧筹码峰整片消失, 退出后又恢复。
根因是 updateChart 里给所有 xAxis 写「未来 bar 槽位」时把筹码叠加的 value 轴
一起写了 —— 筹码轴量纲是权重 (0..~0.1), 被写成 bar 数量 (~500) 后每根柱宽
只剩亚像素, 于是看不见。退出画线 (futureBars=0) 不写 max, 筹码轴恢复原样。

从 index.html 抽取 applyFutureSlots **真实源码**执行 (抽不到即失败),
用真实轴结构 (若干 category 轴 + 一个筹码 value 轴) 断言守卫成立。

运行:
    venv/Scripts/python.exe -u visual/test/test_chip_draw_mode_js.py
"""

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
INDEX_HTML = _VISUAL_DIR / "static" / "index.html"
CHIPS_JS = _VISUAL_DIR / "static" / "js" / "chips.js"


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


class ChipDrawModeStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def test_update_chart_calls_apply_future_slots(self):
        """守卫必须在 updateChart 中生效, 不能被内联回去。"""
        self.assertIn("applyFutureSlots(xAxes, dates, futureBars)", self.src)
        # 旧的裸循环不得残留
        self.assertNotIn(
            "xAxes.forEach(x => { x.data = dates; if (futureBars > 0) x.max",
            self.src,
        )

    def test_guard_skips_non_category_axes(self):
        fn = _extract_fn(self.src, "applyFutureSlots")
        self.assertIn("category", fn, "必须按轴类型守卫")
        self.assertIn("return", fn)

    def test_tooltip_suppressed_in_draw_mode(self):
        """画线模式下点/拖 K 线不给提示框 (触屏落点会被盖住); 十字线读数保留。"""
        body = _extract_fn(self.src, "updateChart")
        self.assertIn("show: !STATE.draw.enabled", body, "tooltip.show 要跟画线模式走")
        self.assertIn("axisPointer", body, "十字线仍要保留")
        # 进入画线模式时把已弹出的提示框立刻收掉 (重绘在下一帧)
        self.assertIn("hideTip", _extract_fn(self.src, "toggleDrawMode"))


class ChipDrawModeBehaviorTest(unittest.TestCase):
    """用真实 index.html 源码 + 真实轴结构跑 applyFutureSlots。"""

    CHIP_XMAX = 0.054  # 权重分数 (chips.py: xdata[i]/total 量级)

    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")
        cls.fn = _extract_fn(cls.src, "applyFutureSlots")

    def _run(self, future_bars, n=500):
        axes = [
            {"gridIndex": 0, "type": "category", "data": [], "show": False},
            {"gridIndex": 1, "type": "category", "data": [], "show": False},
            {"gridIndex": 2, "type": "value", "min": 0, "max": self.CHIP_XMAX, "show": False},
        ]
        script = (
            self.fn + "\n"
            + "const c = JSON.parse(process.argv[1]);"
            + "const dates = Array.from({length: c.n}, (_, i) => '2024-01-' + i);"
            + "applyFutureSlots(c.axes, dates, c.futureBars);"
            + "process.stdout.write(JSON.stringify(c.axes));"
        )
        payload = {"axes": axes, "n": n, "futureBars": future_bars}
        proc = subprocess.run(
            ["node", "-e", script, json.dumps(payload)],
            capture_output=True, check=True,
        )
        return json.loads(proc.stdout.decode("utf-8"))

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_chip_axis_untouched_when_draw_mode_on(self):
        out = self._run(future_bars=8)
        chip = out[2]
        self.assertEqual(chip["max"], self.CHIP_XMAX, "筹码 value 轴 max 被覆盖 → 柱宽变亚像素")
        self.assertEqual(chip["min"], 0)
        self.assertNotIn("data", chip)

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_chip_axis_untouched_when_draw_mode_off(self):
        out = self._run(future_bars=0)
        self.assertEqual(out[2]["max"], self.CHIP_XMAX)

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_category_axes_get_dates_and_future_slots(self):
        out = self._run(future_bars=8, n=500)
        for ax in out[:2]:
            self.assertEqual(len(ax["data"]), 500)
            self.assertEqual(ax["max"], 500 - 1 + 8, "分类轴仍须预留未来 bar 空间")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
    def test_category_axes_max_untouched_when_draw_mode_off(self):
        out = self._run(future_bars=0, n=500)
        for ax in out[:2]:
            self.assertEqual(len(ax["data"]), 500)
            self.assertNotIn("max", ax, "无画线时不应人为限定分类轴 max")

    def test_chip_axis_is_value_type_in_source(self):
        """守卫前提: chips.js 产出的叠加 xAxis 必须是 value 轴。"""
        chips = CHIPS_JS.read_text(encoding="utf-8")
        self.assertIn("type: 'value'", chips)
        self.assertIn("gridIndex: gi", chips)


if __name__ == "__main__":
    unittest.main(verbosity=2)
