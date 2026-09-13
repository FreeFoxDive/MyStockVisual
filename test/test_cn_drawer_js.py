# -*- coding: utf-8 -*-
"""市场数据抽屉宽度自适应测试 (fitCnDrawer)。

需求: 抽屉不再因内容宽而必须横向滑动 —— 默认更宽, 并按表格 scrollWidth 自适应 (上限 92vw)。
从 index.html 抽取 fitCnDrawer **真实源码**在 Node 里注入假 DOM 执行 (抽不到即失败)。

运行:
    venv/Scripts/python.exe -u visual/test/test_cn_drawer_js.py
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


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
class CnDrawerFitTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = INDEX_HTML.read_text(encoding="utf-8")
        cls.script = (_extract_fn(src, "fitCnDrawer") + "\n"
                      + "const c = JSON.parse(process.argv[1]);"
                      # 假 DOM: drawer 收集写入的 width; table 提供 scrollWidth
                      + "globalThis.document = {getElementById: () => c.drawer,"
                      + " querySelector: () => c.table};"
                      + "globalThis.window = {innerWidth: c.innerWidth};"
                      + "fitCnDrawer();"
                      + "process.stdout.write(JSON.stringify(c.drawer.style.width));")

    def _width(self, scroll_width, inner_width, has_table=True):
        drawer = {"style": {"width": "initial"}}
        table = {"scrollWidth": scroll_width} if has_table else None
        proc = subprocess.run(
            ["node", "-e", self.script,
             json.dumps({"drawer": drawer, "table": table, "innerWidth": inner_width})],
            capture_output=True, check=True,
        )
        return json.loads(proc.stdout.decode("utf-8"))

    def test_widens_to_content(self):
        # 700 + 26 = 726, 1920 视口下上限 1766 → 726
        self.assertEqual(self._width(700, 1920), "726px")

    def test_min_width_floor(self):
        # 内容很窄也不小于 440
        self.assertEqual(self._width(100, 1920), "440px")

    def test_capped_by_viewport(self):
        # 视口 400 → 上限 round(400*0.92)=368, 不能超过
        self.assertEqual(self._width(700, 400), "368px")

    def test_no_table_resets_to_css(self):
        # 无表格 (加载中/空/失败) → 清掉内联宽度, 回落 CSS clamp
        self.assertEqual(self._width(0, 1920, has_table=False), "")

    def test_drawer_not_hardcoded_400(self):
        src = INDEX_HTML.read_text(encoding="utf-8")
        start = src.index('id="cn-drawer"')
        style = src[start:start + 320]
        self.assertNotIn("width:400px", style.replace(" ", ""))
        self.assertIn("clamp(", style, "抽屉默认宽度应为 clamp(...) 自适应")


if __name__ == "__main__":
    unittest.main(verbosity=2)
