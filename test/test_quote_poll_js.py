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
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
_TEST_DIR = Path(__file__).resolve().parent
for p in (str(_VISUAL_DIR), str(_TEST_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from js_test_util import InlineScriptParser, require_node, run_node  # noqa: E402

INDEX_HTML = _VISUAL_DIR / "static" / "index.html"


class QuotePollJsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_node()
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _extract(self, name):
        m = re.search(r"^function %s\(.*?\n\}" % re.escape(name), self.src, re.S | re.M)
        self.assertIsNotNone(m, f"index.html 中未找到函数 {name}")
        return m.group(0)

    def test_shared_transport_loaded(self):
        self.assertIn('/js/live-market.js', self.src)
        self.assertIn('/js/market-store.js', self.src)
        self.assertIn('new VisualLive.LiveMarket', self.src)

    def test_main_stream_gate_is_global(self):
        # LiveMarket is constructed before DOMContentLoaded; its active predicate must
        # see the session gate instead of a callback-local declaration.
        gate = self.src.index('function canAutoRefresh()')
        dom_ready = self.src.index("document.addEventListener('DOMContentLoaded'")
        self.assertLess(gate, dom_ready)
        self.assertIn('active: () => typeof canAutoRefresh', self.src)

    def test_period_switch_keeps_panels_and_chart_until_data_arrives(self):
        switch = self._extract("switchPeriod")
        self.assertIn("fetchData({ retainPanels: true })", switch)
        self.assertNotIn("chart.clear", switch)
        self.assertNotIn("fetchStockInfo", switch)
        self.assertNotIn("fetchDepth", switch)
        self.assertIn("function renderFetchedKline", self.src)
        self.assertIn("if (key === STATE._fetchedKlineRenderKey) return false", self.src)

    def test_period_selector_is_fixed_single_row_scroller(self):
        self.assertIn('id="period-viewport"', self.src)
        self.assertIn('id="period-scroll-left"', self.src)
        self.assertIn('id="period-scroll-right"', self.src)
        self.assertIn('function initPeriodScroller()', self.src)
        self.assertNotIn('togglePeriodMore()', self.src)
        self.assertNotIn('class="extra-period"', self.src)

    def test_chart_replace_does_not_clear_canvas_first(self):
        self.assertNotIn('chart.clear()', self.src)
        # 全量重建: notMerge 原子替换 (旧图留到新图就绪, 所以不能先 clear), 且必须同步提交 ——
        # lazyUpdate 会让「model 已换、数据管线没跑」的空窗跨帧, 鼠标事件一碰就抛 TypeError。
        self.assertGreaterEqual(self.src.count('setOption(option, { notMerge: true, silent: true })'), 2)

    def test_inline_scripts_parse(self):
        parser = InlineScriptParser()
        for name in ("index.html", "monitor.html", "trades.html", "screener.html"):
            parser.feed((INDEX_HTML.parent / name).read_text(encoding="utf-8"))
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
