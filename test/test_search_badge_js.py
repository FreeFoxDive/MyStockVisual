# -*- coding: utf-8 -*-
"""搜索徽标 badgeType / searchBadge 前端镜像测试。

从 static/index.html 抽取函数**真实源码**执行 (抽不到即失败), 避免复制一份镜像后与页面漂移。

运行:
    venv/Scripts/python.exe -u visual/test/test_search_badge_js.py
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
    """按大括号配对截取 function name(...) {...} 源码。"""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise AssertionError(f"index.html 中找不到 function {name}")
    start = src.index("{", m.end() - 1)
    depth = 0
    for i in range(start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():i + 1]
    raise AssertionError(f"function {name} 大括号不配对")


@unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端镜像测试")
class SearchBadgeJsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = INDEX_HTML.read_text(encoding="utf-8")
        cls.script = (_extract_fn(src, "badgeType") + "\n"
                      + _extract_fn(src, "searchBadge") + "\n"
                      + "const cases = JSON.parse(process.argv[1]);"
                      + "process.stdout.write(JSON.stringify(cases.map(c =>"
                      + " ({type: badgeType(c), badge: searchBadge(c)}))));")

    def _badges(self, cases):
        proc = subprocess.run(
            ["node", "-e", self.script, json.dumps(cases)],
            capture_output=True, check=True,
        )
        return json.loads(proc.stdout.decode("utf-8"))

    def test_type_takes_precedence(self):
        out = self._badges([
            {"symbol": "000001.SH", "type": "index"},
            {"symbol": "512800.SH", "type": "etf"},
            # 指数/ETF 优先于代码形态判定
            {"symbol": "00700.HK", "type": "etf"},
            {"symbol": "TSLA", "type": "index"},
        ])
        self.assertEqual([o["badge"] for o in out], ["指数", "ETF", "ETF", "指数"])

    def test_hk_by_symbol_shape(self):
        out = self._badges([
            {"symbol": "00700.HK", "type": "stock"},
            {"symbol": "09988.hk", "type": "stock"},
        ])
        self.assertEqual([o["type"] for o in out], ["hk", "hk"])
        self.assertEqual([o["badge"] for o in out], ["港", "港"])

    def test_us_by_alnum_code(self):
        out = self._badges([
            {"symbol": "AAPL", "type": "stock"},
            {"symbol": "aapl", "type": "stock"},
            {"symbol": "BRK.B", "type": "stock"},
        ])
        self.assertEqual([o["type"] for o in out], ["us", "us", "us"])
        self.assertEqual([o["badge"] for o in out], ["美", "美", "美"])

    def test_a_share_has_no_badge(self):
        out = self._badges([
            {"symbol": "600000.SH", "type": "stock"},
            {"symbol": "000001.SZ", "type": "stock"},
            {"symbol": "430047.BJ", "type": "stock"},
            {"symbol": "", "type": "stock"},
            {"type": "stock"},
        ])
        self.assertEqual([o["badge"] for o in out], ["", "", "", "", ""])
        self.assertEqual([o["type"] for o in out], ["", "", "", "", ""])


if __name__ == "__main__":
    unittest.main(verbosity=2)
