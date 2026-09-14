# -*- coding: utf-8 -*-
"""localStorage 缓存淘汰: _cacheEntryTs / _cleanLocalStorage 抽取真实函数测试。

/vk_ 条目是整包 /api/kline 响应 (约 600KB), 淘汰时只为读一个 ts 而整包
JSON.parse 是纯浪费; 改为只匹配开头的 {"ts":<数字>。这里锁定该行为与淘汰策略。

运行:
    venv/Scripts/python.exe -u visual/test/test_ls_cache_js.py
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
_TEST_DIR = Path(__file__).resolve().parent
for p in (str(_VISUAL_DIR), str(_TEST_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from js_test_util import require_node, run_node  # noqa: E402

INDEX_HTML = _VISUAL_DIR / "static" / "index.html"


class LsCacheJsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_node()
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _extract(self, name):
        m = re.search(r"^function %s\(.*?\n\}" % re.escape(name), self.src, re.S | re.M)
        self.assertIsNotNone(m, f"index.html 中未找到函数 {name}")
        return m.group(0)

    def _run(self, entries, budget, keep=("visual-chart-history",)):
        js = (
            "const INPUT = JSON.parse(process.argv[1]);\n"
            "const store = new Map(Object.entries(INPUT.entries));\n"
            "const localStorage = {\n"
            "  get length() { return store.size; },\n"
            "  key: (i) => Array.from(store.keys())[i],\n"
            "  getItem: (k) => (store.has(k) ? store.get(k) : null),\n"
            "  removeItem: (k) => { store.delete(k); },\n"
            "};\n"
            "const LS_CACHE_BUDGET = INPUT.budget;\n"
            "const LS_KEEP_KEYS = new Set(INPUT.keep);\n"
            + self._extract("_cacheEntryTs") + "\n"
            + self._extract("_cleanLocalStorage") + "\n"
            + "_cleanLocalStorage();\n"
            + "process.stdout.write(JSON.stringify(Array.from(store.keys())));"
        )
        payload = {"entries": entries, "budget": budget, "keep": list(keep)}
        return json.loads(run_node(js, json.dumps(payload)))

    def test_ts_read_without_parsing_the_whole_payload(self):
        """ts 之后的 JSON 即使损坏, 也必须能读出淘汰所需的 ts (不再整包 parse)。"""
        js = (
            self._extract("_cacheEntryTs") + "\n"
            "process.stdout.write(String(_cacheEntryTs(process.argv[1])));"
        )
        broken = '{"ts":1757894400123,"data":{"klines":[' + "x" * 200
        self.assertEqual(run_node(js, broken).strip(), "1757894400123")

    def test_ts_defaults_to_zero_when_absent(self):
        js = (
            self._extract("_cacheEntryTs") + "\n"
            "process.stdout.write(String(_cacheEntryTs(process.argv[1])));"
        )
        self.assertEqual(run_node(js, "not-json").strip(), "0")
        self.assertEqual(run_node(js, "").strip(), "0")

    def test_under_budget_keeps_everything(self):
        entries = {"vk_a_1d": '{"ts":100,' + "x" * 90,
                   "vk_b_1d": '{"ts":200,' + "x" * 90}
        self.assertEqual(sorted(self._run(entries, 100000)), ["vk_a_1d", "vk_b_1d"])

    def test_evicts_oldest_first_until_under_budget(self):
        entries = {"vk_a_1d": '{"ts":100,' + "x" * 96,
                   "vk_b_1d": '{"ts":200,' + "x" * 96}
        out = self._run(entries, 150)
        self.assertEqual(out, ["vk_b_1d"], "应先淘汰最旧的一条")

    def test_keep_keys_never_evicted(self):
        entries = {"visual-chart-history": "y" * 400,
                   "vk_a_1d": '{"ts":100,' + "x" * 96}
        out = self._run(entries, 10)
        self.assertIn("visual-chart-history", out, "配置/历史键不得被淘汰")
        self.assertNotIn("vk_a_1d", out)

    def test_non_cache_keys_ignored_but_counted(self):
        """非缓存键不计入淘汰候选, 但仍计入总量。"""
        entries = {"other_key": "z" * 500, "vk_a_1d": '{"ts":100,' + "x" * 90}
        out = self._run(entries, 10)
        self.assertIn("other_key", out)
        self.assertNotIn("vk_a_1d", out)


if __name__ == "__main__":
    unittest.main()
