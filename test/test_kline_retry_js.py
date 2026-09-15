# -*- coding: utf-8 -*-
"""K线加载健壮性: fetchKlineWithRetry 从 static/index.html 抽取真实函数测试。

背景: 源链瞬时耗尽 (冷启动/上游抖动) 曾让单次 404 黑屏到手动刷新。
契约: 404/网络错误有界重试 (退避 1s/2s), 换股 (_fetchTs 变化) 或 abort 立即中止;
scheduleKlineRecovery 借 tail 成功补拉整图 (15s 冷却 + 连续失败 5 次熔断)。

运行:
    venv/Scripts/python.exe -u visual/test/test_kline_retry_js.py
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


class KlineRetryJsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_node()
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _extract(self, name):
        m = re.search(r"^async function %s\(.*?\n\}" % re.escape(name), self.src, re.S | re.M)
        if m is None:
            m = re.search(r"^function %s\(.*?\n\}" % re.escape(name), self.src, re.S | re.M)
        self.assertIsNotNone(m, f"index.html 中未找到函数 {name}")
        return m.group(0)

    def _run_retry(self, script, calls):
        js = (
            "const STATE = {_fetchTs: 1};\n"
            + self._extract("fetchKlineWithRetry") + "\n"
            + "const steps = JSON.parse(process.argv[2]);\n"
            + "let n = 0;\n"
            + "global.fetch = (url, opts) => {\n"
            + "  const step = steps[Math.min(n, steps.length - 1)]; n++;\n"
            + "  if (step === 'e404') return Promise.resolve({ok: false, status: 404,"
            " json: () => Promise.resolve({error: 'x'})});\n"
            + "  if (step === 'abort') { const e = new Error('aborted'); e.name = 'AbortError';"
            " return Promise.reject(e); }\n"
            + "  if (step === 'ok') return Promise.resolve({ok: true, status: 200,"
            " json: () => Promise.resolve({ok_data: true})});\n"
            + "  return Promise.resolve({ok: false, status: 500, json: () => Promise.resolve({})});\n"
            + "};\n"
            + "const nRef = { get: () => n };\n"
            + script
        )
        return json.loads(run_node(js, "x", json.dumps(calls)))

    def test_retry_succeeds_on_second_attempt(self):
        out = self._run_retry(
            "fetchKlineWithRetry('/u', null, 1, 3).then(d =>"
            " process.stdout.write(JSON.stringify({d, calls: nRef.get()})));",
            ["e404", "ok"])
        self.assertEqual(out["d"], {"ok_data": True})
        self.assertEqual(out["calls"], 2, "首次 404 后应重试并成功")

    def test_retry_gives_up_after_attempts(self):
        out = self._run_retry(
            "fetchKlineWithRetry('/u', null, 1, 3).catch(e =>"
            " process.stdout.write(JSON.stringify({err: String(e), calls: nRef.get()})));",
            ["e500"])
        self.assertEqual(out["calls"], 3, "有界: 最多 attempts 次")
        self.assertIn("HTTP 500", out["err"])

    def test_retry_skipped_when_fetch_ts_changed(self):
        # _fetchTs 已前进 (用户换股): 不应发出任何 fetch, 直接抛出取代错误
        js = (
            "const STATE = {_fetchTs: 2};\n"
            + self._extract("fetchKlineWithRetry") + "\n"
            + "let n = 0;\n"
            + "global.fetch = () => { n++; return Promise.resolve({ok: true,"
            " json: () => Promise.resolve({})}); };\n"
            + "fetchKlineWithRetry('/u', null, 1, 3).catch(e =>"
            " process.stdout.write(JSON.stringify({err: String(e), calls: n})));"
        )
        out = json.loads(run_node(js))
        self.assertEqual(out["calls"], 0, "换股后不重试")
        self.assertIn("取代", out["err"])

    def test_retry_aborts_immediately(self):
        out = self._run_retry(
            "fetchKlineWithRetry('/u', null, 1, 3).catch(e =>"
            " process.stdout.write(JSON.stringify({name: e.name, calls: nRef.get()})));",
            ["abort"])
        self.assertEqual(out["name"], "AbortError")
        self.assertEqual(out["calls"], 1, "abort 不重试")

    def test_schedule_kline_recovery_cooldown_and_fuse(self):
        js = (
            "const STATE = {_fetchTs: 1, _klineRecoverFails: 0};\n"
            + "const loadCalls = [];\n"
            + self.src[self.src.index("let _klineRecoverAt"): self.src.index("const liveQuotes")] + "\n"
            + "function loadCurrent() { loadCalls.push(1); }\n"
            + "scheduleKlineRecovery();\n"
            + "scheduleKlineRecovery();\n"  # 冷却期内: 不应再次触发
            + "const afterCooldown = loadCalls.length;\n"
            + "STATE._klineRecoverFails = 5;\n"
            + "_klineRecoverAt = 0;\n"
            + "scheduleKlineRecovery();\n"
            + "process.stdout.write(JSON.stringify({afterCooldown, finalCalls: loadCalls.length}));"
        )
        out = json.loads(run_node(js))
        self.assertEqual(out["afterCooldown"], 1, "15s 冷却内只补拉一次")
        self.assertEqual(out["finalCalls"], 1, "连续失败 5 次后熔断, 不再自动补拉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
