# -*- coding: utf-8 -*-
"""换股取消在途 /api/kline: 源码契约测试 (fetchData 依赖大量 DOM, 不做整段求值)。

仅靠 _fetchTs 丢弃响应不够 —— 请求本身仍占住 waitress 线程 (只有 8 个),
连续切换会互相排队。这里锁定: 换股/切周期主动 abort、signal 透传、
AbortError 不算失败、且被取代的请求不得清掉新请求的加载态。

运行:
    venv/Scripts/python.exe -u visual/test/test_fetch_cancel_js.py
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
_TEST_DIR = Path(__file__).resolve().parent
for p in (str(_VISUAL_DIR), str(_TEST_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

INDEX_HTML = _VISUAL_DIR / "static" / "index.html"


class FetchCancelContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = INDEX_HTML.read_text(encoding="utf-8")
        m = re.search(r"^async function fetchData\(.*?\n\}\n", cls.src, re.S | re.M)
        assert m is not None, "index.html 中未找到 fetchData"
        cls.fetch_data = m.group(0)

    def test_state_declares_kline_abort(self):
        self.assertIn("_klineAbort", self.src)

    def test_aborts_previous_request_before_fetching(self):
        self.assertIn("STATE._klineAbort.abort()", self.fetch_data)
        # 必须在发起新请求之前取消, 而不是仅丢弃响应
        self.assertLess(self.fetch_data.index("STATE._klineAbort.abort()"),
                        self.fetch_data.index("await fetch("))

    def test_abort_controller_is_replaced_each_load(self):
        self.assertIn("STATE._klineAbort = new AbortController()", self.fetch_data)

    def test_signal_passed_to_kline_fetch(self):
        m = re.search(r"await fetch\(url,[^)]*signal", self.fetch_data, re.S)
        self.assertIsNotNone(m, "/api/kline 未把 AbortController.signal 传给 fetch")

    def test_background_refresh_does_not_abort_foreground(self):
        """silent 后台刷新不得取消前台请求。"""
        idx = self.fetch_data.index("STATE._klineAbort.abort()")
        guard = self.fetch_data[:idx]
        self.assertIn("if (!silent)", guard,
                      "abort 必须受 !silent 保护, 否则后台刷新会打断前台加载")

    def test_aborterror_is_not_reported_as_failure(self):
        self.assertIn("e.name === 'AbortError'", self.fetch_data)
        idx = self.fetch_data.index("AbortError")
        after = self.fetch_data[idx:]
        # 取消应静默返回, 不能走到错误提示
        self.assertLess(after.index("return"), after.index("showToast"))

    def test_superseded_request_does_not_clear_loading(self):
        """被取代的请求 finally 不得清掉新请求刚显示的 loading。"""
        m = re.search(r"\} finally \{(.*?)\n  \}", self.fetch_data, re.S)
        self.assertIsNotNone(m, "fetchData 缺少 finally 收尾")
        self.assertIn("STATE._fetchTs === fetchTs", m.group(1),
                      "finally 必须只在仍是当前请求时收尾")


if __name__ == "__main__":
    unittest.main()
