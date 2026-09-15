# -*- coding: utf-8 -*-
"""交易页/监控页现价快照兜底的 JS 回归测试。

背景 (bug): commit 9f6e9f9 给 LiveMarket 加了 inAshareSession 门控后, 盘后/午休
tick() 直接 return, 一个行情请求都不发, 交易记录「现价」整列 —; 且交易页翻页/
筛选后 loadTrades 不重订阅, 当前页平仓标的盘中也无现价。

回归点:
  * trades.html loadTrades 必须调用 refreshQuotes (翻页/筛选后重订阅);
  * trades.refreshQuotes / monitor.loadQuotes 必须带不受门控的 /api/quotes
    快照兜底, 且经 liveQuotes.accept('quote', …, 'embedded') 走统一通道;
  * 两页 active 门控必须保留 (盘中续刷仍受控, 盘后不得持续轮询)。

运行:
    venv/Scripts/python.exe -m unittest visual/test/test_trades_quotes_js.py -v
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
_TRADES_HTML = _VISUAL_DIR / "static" / "trades.html"
_MONITOR_HTML = _VISUAL_DIR / "static" / "monitor.html"

for p in (str(_VISUAL_DIR), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

from js_test_util import require_node, run_node  # noqa: E402


def _extract_fn(src: str, name: str) -> str:
    """按大括号配对抽取函数源码 (保留 async 前缀)。"""
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    if not m:
        raise AssertionError(f"未找到 function {name}")
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


class TradesQuotesStaticTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trades = _TRADES_HTML.read_text(encoding="utf-8")
        cls.monitor = _MONITOR_HTML.read_text(encoding="utf-8")

    def test_load_trades_resubscribes_quotes(self):
        body = _extract_fn(self.trades, "loadTrades")
        self.assertIn("refreshQuotes", body, "翻页/筛选后必须重订阅现价")

    def test_trades_refresh_quotes_has_snapshot_fallback(self):
        body = _extract_fn(self.trades, "refreshQuotes")
        self.assertIn("setSymbols", body)
        self.assertIn("fetchQuoteSnapshot", body)
        snap = _extract_fn(self.trades, "fetchQuoteSnapshot")
        self.assertIn("/api/quotes", snap, "需要不受门控的直连快照兜底")
        self.assertIn("accept('quote', s, q, 'embedded')", snap, "须走统一 accept 通道")

    def test_monitor_load_quotes_has_snapshot_fallback(self):
        body = _extract_fn(self.monitor, "loadQuotes")
        self.assertIn("setSymbols", body)
        self.assertIn("/api/quotes", body, "需要不受门控的直连快照兜底")
        self.assertIn('accept("quote", s, q, "embedded")', body, "须走统一 accept 通道")

    def test_session_gates_kept(self):
        # 门控保留: 盘中由 LiveMarket 续刷, 盘后靠快照兜底而非持续轮询
        self.assertIn("active: () => !!state.user && inAshareSession()", self.trades)
        self.assertIn("active: () => inAshareSession()", self.monitor)


class TradesQuotesBehaviorTest(unittest.TestCase):
    """用 stub 全局跑真实抽取函数, 断言快照兜底的请求/灌入行为。"""

    @classmethod
    def setUpClass(cls):
        require_node()
        cls.trades = _TRADES_HTML.read_text(encoding="utf-8")
        cls.monitor = _MONITOR_HTML.read_text(encoding="utf-8")

    def test_snapshot_fallback_behavior(self):
        script = """
const results = {};
(async () => {
  // ── trades.html refreshQuotes: 持仓 + 当前页平仓, 快照灌入 accept ──
  {
    const rec = { apiPaths: [], accepts: [], setSymbols: [], refresh: 0 };
    const state = {
      stats: { open_positions: [{ symbol: '600000.SH' }, { symbol: '600000.SH' }] },
      trades: [{ symbol: '000001.SZ', status: 'closed' }, { symbol: '000002.SZ', status: 'open' }],
    };
    const liveQuotes = {
      setSymbols: s => rec.setSymbols.push(s),
      refresh: () => rec.refresh++,
      accept: (kind, s, q, src) => rec.accepts.push([s, src]),
    };
    const api = async path => {
      rec.apiPaths.push(path);
      return { '600000.SH': { last_price: 10.5 }, '000001.SZ': { last_price: 12 } };
    };
    %(trades_refresh)s
    %(trades_snapshot)s
    await refreshQuotes(false);
    await refreshQuotes(true);
    results.trades = rec;
  }
  // ── trades.html: 无标的时直连请求不应发生 ──
  {
    const rec = { apiPaths: [], accepts: [] };
    const state = { stats: { open_positions: [] }, trades: [] };
    const liveQuotes = {
      setSymbols: () => {}, refresh: () => {},
      accept: (kind, s, q, src) => rec.accepts.push([s, src]),
    };
    const api = async path => { rec.apiPaths.push(path); return {}; };
    %(trades_refresh)s
    %(trades_snapshot)s
    await refreshQuotes(true);
    results.tradesEmpty = rec;
  }
  // ── trades.html: 快照请求失败不外抛, 不灌入 ──
  {
    const rec = { apiPaths: [], accepts: [], threw: false };
    const state = { stats: { open_positions: [{ symbol: '600000.SH' }] }, trades: [] };
    const liveQuotes = {
      setSymbols: () => {}, refresh: () => {},
      accept: (kind, s, q, src) => rec.accepts.push([s, src]),
    };
    const api = async path => { rec.apiPaths.push(path); throw new Error('boom'); };
    %(trades_refresh)s
    %(trades_snapshot)s
    try { await refreshQuotes(false); } catch (e) { rec.threw = true; }
    results.tradesFail = rec;
  }
  // ── monitor.html loadQuotes: 持仓快照兜底 ──
  {
    const rec = { apiPaths: [], accepts: [], setSymbols: [] };
    const liveQuotes = {
      setSymbols: s => rec.setSymbols.push(s), refresh: () => {},
      accept: (kind, s, q, src) => rec.accepts.push([s, src]),
    };
    const api = async path => {
      rec.apiPaths.push(path);
      return { '600519.SH': { last_price: 1700 } };
    };
    %(monitor_load)s
    await loadQuotes([{ symbol: '600519.SH' }]);
    await loadQuotes([]);
    results.monitor = rec;
  }
})().then(() => console.log(JSON.stringify(results)))
    .catch(e => { console.error(e && e.stack || e); process.exit(1); });
""" % {
            "trades_refresh": _extract_fn(self.trades, "refreshQuotes"),
            "trades_snapshot": _extract_fn(self.trades, "fetchQuoteSnapshot"),
            "monitor_load": _extract_fn(self.monitor, "loadQuotes"),
        }
        data = json.loads(run_node(script))

        t = data["trades"]
        self.assertEqual(len(t["apiPaths"]), 2)
        for path in t["apiPaths"]:
            self.assertTrue(path.startswith("/api/quotes?symbols="), path)
            self.assertIn("600000.SH", path)
            self.assertIn("000001.SZ", path)
        self.assertEqual(t["setSymbols"], [
            ["600000.SH", "600000.SH", "000001.SZ"],
            ["600000.SH", "600000.SH", "000001.SZ"],
        ], "订阅列表 = 持仓 + 当前页平仓 (open 标的不入)")
        self.assertEqual(t["refresh"], 1, "仅 fresh=true 时触发 liveQuotes.refresh")
        self.assertEqual(t["accepts"], [
            ["600000.SH", "embedded"], ["000001.SZ", "embedded"],
            ["600000.SH", "embedded"], ["000001.SZ", "embedded"],
        ])

        te = data["tradesEmpty"]
        self.assertEqual(te["apiPaths"], [], "空订阅不发请求")
        self.assertEqual(te["accepts"], [])

        tf = data["tradesFail"]
        self.assertFalse(tf["threw"], "快照失败不能外抛中断 refreshQuotes")
        self.assertEqual(tf["accepts"], [])

        m = data["monitor"]
        self.assertEqual(len(m["apiPaths"]), 1, "空持仓不发请求")
        self.assertTrue(m["apiPaths"][0].startswith("/api/quotes?symbols="))
        self.assertEqual(m["setSymbols"], [["600519.SH"], []])
        self.assertEqual(m["accepts"], [["600519.SH", "embedded"]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
