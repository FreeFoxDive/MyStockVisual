# -*- coding: utf-8 -*-
"""选股页 (screener.html) 的时段门控与进度轮询回归测试。

两起真实问题:
  * 结果表现价由 LiveMarket 刷新, 但本页**没有任何 active 门控** —— 选股页开着
    就全天每 2.5s 轮询一次 /api/quotes (盘外服务端只返回收盘快照, 纯浪费), 比
    整条行情链路的探测预算还高;
  * pollStatus 的 catch 里置 scanning=false, 而兜底 interval 又以 `if (scanning)`
    为条件 —— 一次 /api/screener/status 抖动就永久冻住进度, 只能刷新页面。

运行:
    python -m unittest discover -s test -p "test_screener_page_js.py"
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
SCREENER_HTML = _VISUAL_DIR / "static" / "screener.html"


def _extract_fn(src: str, name: str) -> str:
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise AssertionError(f"screener.html 中找不到 function {name}")
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


class ScreenerPageStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = SCREENER_HTML.read_text(encoding="utf-8")

    def test_quotes_are_session_gated(self):
        """盘外不该持续轮询现价 (服务端那会儿只返回收盘快照)。"""
        self.assertIn("/js/market-clock.js", self.src, "要用共享时段口径")
        self.assertIn("new VisualMarketClock.MarketClock(", self.src)
        self.assertIn("marketClock.start()", self.src)
        self.assertRegex(self.src, r"active: \(\) =>[^\n]*canAutoRefresh\(\)")
        self.assertRegex(self.src, r"streamActive: \(\) =>[^\n]*canStreamQuotes\(\)")
        for name in ("canAutoRefresh", "canStreamQuotes"):
            body = _extract_fn(self.src, name)
            self.assertIn("document.hidden", body, "隐藏标签页必须停机")
            self.assertIn("marketClock.", body, "时段口径只来自时钟")

    def test_clock_state_reaches_the_page(self):
        self.assertIn("onMarket: payload => marketClock.applyMarketFrame(payload)", self.src)
        self.assertIn("onStreamState: open => marketClock.setStreamConnected", self.src)
        # 与其他三页同一套: 恢复事件要立刻重刷现价
        clock = self.src[self.src.index("new VisualMarketClock.MarketClock("):]
        clock = clock[:clock.index("});")]
        self.assertIn("kind === 'resume'", clock)
        self.assertIn("liveQuotes.refresh()", clock)

    def test_poll_state_declared_before_use(self):
        """pollStatus 会被 runScan/initResume 提前调用, 计数必须在它们之前声明。

        (原实现把它放在 pollStatus 上方靠后的位置, 只靠"调用点前面正好有 await"
        侥幸不炸 —— 去掉那个 await 就是 TDZ 报错。)
        """
        src = self.src
        decl = src.index("let pollFailures = 0;")
        for caller in ("async function runScan(", "(async function initResume()"):
            self.assertLess(decl, src.index(caller),
                            f"pollFailures 必须声明在 {caller} 之前")
        self.assertEqual(src.count("let pollFailures"), 1, "只能声明一次")

    def test_no_duplicate_status_poller(self):
        """1.5s 自排链已经覆盖扫描期; 再来一个 3s interval 只是白多一倍请求。"""
        self.assertNotIn("setInterval(() => { if (scanning) pollStatus(); }, 3000)", self.src)

    def test_poll_status_keeps_scanning_on_error(self):
        body = _extract_fn(self.src, "pollStatus")
        catch = body[body.index("} catch"):]
        self.assertNotIn("scanning = false", catch,
                         "取状态失败不代表扫描结束: 置假会让进度永久冻住")
        self.assertIn("setTimeout(pollStatus", catch, "要退避重试")


class ScreenerPollStatusBehaviorTest(unittest.TestCase):
    """用 stub 跑真实 pollStatus, 断言重排节奏与失败后的自愈。"""

    @classmethod
    def setUpClass(cls):
        src = SCREENER_HTML.read_text(encoding="utf-8")
        cls.fn = _extract_fn(src, "pollStatus")

    def _run(self, responses, drain=0):
        """responses: 依次返回的状态载荷, 'throw' 表示该次请求失败。
        drain: 轮询自己排下的重试再执行几次 (0 = 只看第一次)。"""
        script = (
            "const results = {};\n"
            "globalThis.__scheduled = [];\n"
            "globalThis.__queue = [];\n"
            "globalThis.setTimeout = (fn, ms) => { globalThis.__scheduled.push(ms);"
            " globalThis.__queue.push(fn); return 1; };\n"
            "globalThis.scanning = true;\n"
            "globalThis.setStopBtn = () => { results.stopBtn = (results.stopBtn || 0) + 1; };\n"
            "globalThis.renderResults = (r) => { results.rendered = r; };\n"
            "globalThis.document = { getElementById: () => ({ style: {}, textContent: '' }) };\n"
            "const seq = " + json.dumps(responses) + ";\n"
            "let i = 0;\n"
            "globalThis.api = async () => {\n"
            "  const r = seq[Math.min(i++, seq.length - 1)];\n"
            "  if (r === 'throw') throw new Error('status boom');\n"
            "  return r;\n"
            "};\n"
            "globalThis.pollFailures = 0;\n"
            + self.fn + "\n"
            + "(async () => {\n"
            + "  await pollStatus();\n"
            + f"  for (let k = 0; k < {drain} && globalThis.__queue.length; k++) {{\n"
            + "    await globalThis.__queue.shift()();\n"
            + "  }\n"
            + "  process.stdout.write(JSON.stringify({ scheduled: globalThis.__scheduled,"
            + " scanning: globalThis.scanning, failures: globalThis.pollFailures,"
            + " stopBtn: results.stopBtn || 0, rendered: results.rendered || null }));\n"
            + "})();"
        )
        proc = subprocess.run(["node", "-e", script], capture_output=True,
                              check=True, text=True, encoding="utf-8")
        return json.loads(proc.stdout)

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端行为测试")
    def test_running_reschedules_fast(self):
        out = self._run([{"running": True, "progress": 3, "total": 10, "results": []}])
        self.assertEqual(out["scheduled"], [1500])
        self.assertTrue(out["scanning"], "扫描中不得改状态")

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端行为测试")
    def test_finished_renders_and_stops(self):
        out = self._run([{"running": False, "progress": 10, "total": 10,
                          "results": [{"symbol": "600000.SH"}]}])
        self.assertEqual(out["scheduled"], [], "扫描结束不再轮询")
        self.assertFalse(out["scanning"])
        self.assertEqual(out["stopBtn"], 1)
        self.assertEqual(out["rendered"], [{"symbol": "600000.SH"}])

    @unittest.skipUnless(shutil.which("node"), "需要 node 才能跑前端行为测试")
    def test_error_backs_off_and_recovers(self):
        """两次失败仍算扫描中且间隔递增; 之后成功 → 复位回 1.5s 节奏。"""
        out = self._run(
            ["throw", "throw", {"running": True, "progress": 1, "total": 4, "results": []}],
            drain=2)
        self.assertTrue(out["scanning"], "取状态失败不该判成扫描结束")
        self.assertEqual(out["scheduled"], [1500, 3000, 1500],
                         "失败退避 1.5s→3s, 恢复后回到 1.5s")
        self.assertEqual(out["failures"], 0, "成功即复位失败计数")
        self.assertEqual(out["stopBtn"], 0, "还在扫描, 不该把停止按钮收掉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
