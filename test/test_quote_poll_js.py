# -*- coding: utf-8 -*-
"""快照轮询/SSE 决策的 JS 单元测试。

直接从 static/index.html 抽取真实函数(而非手抄镜像), 并断言源码含 SSE 守卫,
避免镜像与实现漂移。依赖 node; 无 node 时自动 skip。

运行:
    venv/Scripts/python.exe -m unittest visual/test/test_quote_poll_js.py -v
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
_TEST_DIR = Path(__file__).resolve().parent
for p in (str(_VISUAL_DIR), str(_TEST_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from js_test_util import require_node, run_node  # noqa: E402

INDEX_HTML = _VISUAL_DIR / "static" / "index.html"


class _InlineScriptParser(HTMLParser):
    """提取内联 <script> 正文 (跳过带 src 的外链脚本)。"""

    def __init__(self):
        super().__init__()
        self.blocks = []
        self._buf = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "script" and not any(k.lower() == "src" for k, _ in attrs):
            self._buf = []

    def handle_data(self, data):
        if self._buf is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._buf is not None:
            self.blocks.append("".join(self._buf))
            self._buf = None


class QuotePollJsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_node()
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _extract(self, name):
        m = re.search(r"^function %s\(.*?\n\}" % re.escape(name), self.src, re.S | re.M)
        self.assertIsNotNone(m, f"index.html 中未找到函数 {name}")
        return m.group(0)

    def test_source_has_sse_guards(self):
        for needle in ("EventSource.OPEN", "quotePollDecision", "quoteStreamRetryDue",
                       "SSE_STALE_FALLBACK_MS", "SSE_RETRY_MS"):
            self.assertIn(needle, self.src, f"index.html 缺少 {needle}")

    def test_decision_matrix(self):
        js = (
            "const SSE_STALE_FALLBACK_MS=30000;\n"
            "const QUOTE_REFRESH_MS=10000;\n"
            "const SSE_RETRY_MS=60000;\n"
            + self._extract("quotePollDecision") + "\n"
            + self._extract("quoteStreamRetryDue") + "\n"
            + """
const out = [];
const now = 1000000;
out.push(quotePollDecision(now, now - 5000, true));    // SSE OPEN 5s -> 不轮询
out.push(quotePollDecision(now, now - 40000, true));   // SSE OPEN 40s(僵尸) -> 兜底并重建
out.push(quotePollDecision(now, now - 15000, false));  // 非 OPEN 15s -> 轮询
out.push(quotePollDecision(now, now - 5000, false));   // 非 OPEN 5s -> 不轮询
out.push(quoteStreamRetryDue(now, true, 0, 0, true));                  // 有 es -> 不重建
out.push(quoteStreamRetryDue(now, false, now - 70000, 60000, true));   // 过退避 -> 重建
out.push(quoteStreamRetryDue(now, false, now - 10000, 60000, true));   // 未过退避 -> 不重建
out.push(quoteStreamRetryDue(now, false, 0, 0, false));                // 不可刷新 -> 不重建
process.stdout.write(JSON.stringify(out));
"""
        )
        got = json.loads(run_node(js))
        self.assertEqual(
            got,
            [None, "poll_rotate", "poll", None, False, True, False, False],
        )

    def test_inline_scripts_parse(self):
        parser = _InlineScriptParser()
        parser.feed(self.src)
        blocks = parser.blocks
        self.assertTrue(blocks, "未找到 inline script")
        for i, body in enumerate(blocks):
            with tempfile.NamedTemporaryFile(
                "w", suffix=".js", delete=False, encoding="utf-8"
            ) as f:
                f.write(body)
                path = f.name
            try:
                proc = subprocess.run(
                    ["node", "--check", path],
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                )
                self.assertEqual(proc.returncode, 0,
                                 f"inline script #{i} 语法错误: {proc.stderr}")
            finally:
                os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
