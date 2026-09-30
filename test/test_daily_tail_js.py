# -*- coding: utf-8 -*-
"""日K末根增量: applyServerBars 从 static/index.html 抽取真实函数测试。

契约: 有没有当日 bar、以及全部指标只由后端产出。前端按 date 合并/追加,
不新增 bar; 同日快照只刷新已存在 bar 的 OHLCV (patchTodayBarOhlcv)。
非交易日/盘前后端不拼当日 bar, 前端按 date 合并不会凭空多一根。

运行:
    venv/Scripts/python.exe -u visual/test/test_daily_tail_js.py
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


class DailyTailJsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_node()
        cls.src = INDEX_HTML.read_text(encoding="utf-8")

    def _extract(self, name):
        m = re.search(r"^function %s\(.*?\n\}" % re.escape(name), self.src, re.S | re.M)
        self.assertIsNotNone(m, f"index.html 中未找到函数 {name}")
        return m.group(0)

    def _run_apply(self, klines, bars, period="1d"):
        js = (
            "function STATE_(){return %s;}\n" % json.dumps({"period": period, "klineData": {"klines": klines}})
            + "let STATE = STATE_();\n"
            + self._extract("applyServerBars") + "\n"
            + "const bars = JSON.parse(process.argv[1]);\n"
            + "const changed = applyServerBars(bars);\n"
            + "process.stdout.write(JSON.stringify({changed, klines: STATE.klineData.klines}));"
        )
        return json.loads(run_node(js, json.dumps(bars)))

    def test_source_no_longer_synthesizes_bar(self):
        # 前端不得再从快照新增 bar, 也不得再本地重算指标。
        # 同日 OHLCV 刷新走 patchTodayBarOhlcv, 不恢复被禁的旧函数。
        for gone in ("patchTodayBarFromQuote", "recalcTailIndicators", "todayYMD"):
            self.assertNotIn(gone, self.src, f"index.html 仍含已废弃的 {gone}")
        self.assertIn("function patchTodayBarOhlcv", self.src)
        self.assertIn("/api/kline/tail", (INDEX_HTML.parent / "js/live-market.js").read_text(encoding="utf-8"))

    def test_append_new_trading_day_bar(self):
        klines = [{"date": "2026-09-11", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000}]
        bar = {"date": "2026-09-14", "open": 10.8, "high": 11.2, "low": 10.7,
               "close": 11.0, "volume": 2000, "ma5": 10.6, "macd_dif": 0.1}
        out = self._run_apply(klines, [bar])
        self.assertTrue(out["changed"])
        self.assertEqual(len(out["klines"]), 2)
        self.assertEqual(out["klines"][-1]["date"], "2026-09-14")
        self.assertEqual(out["klines"][-1]["ma5"], 10.6)

    def test_merge_same_day_bar_keeps_unmanaged_fields(self):
        # 服务端未返回 premium 时, 合并不得丢掉原字段 (ETF 溢价)
        klines = [{"date": "2026-09-11", "open": 10, "high": 11, "low": 9, "close": 10.5,
                   "volume": 1000, "premium": 1.23}]
        bar = {"date": "2026-09-11", "open": 10, "high": 11.5, "low": 9, "close": 11.0,
               "volume": 1500, "ma5": 10.7}
        out = self._run_apply(klines, [bar])
        self.assertTrue(out["changed"])
        self.assertEqual(len(out["klines"]), 1)
        last = out["klines"][-1]
        self.assertEqual(last["close"], 11.0)
        self.assertEqual(last["ma5"], 10.7)
        self.assertEqual(last["premium"], 1.23, "合并不得丢掉服务端未提供的原字段")

    def test_non_trading_day_same_last_bar_is_noop(self):
        # 非交易日: 后端返回的末根 == 图表末根 (最近交易日) → 无变化, 不多一根
        klines = [{"date": "2026-09-10", "open": 36.9, "high": 36.9, "low": 36.0, "close": 36.2, "volume": 122},
                  {"date": "2026-09-11", "open": 36.1, "high": 36.1, "low": 34.1, "close": 34.6, "volume": 280}]
        same = dict(klines[-1])
        out = self._run_apply(klines, [same])
        self.assertFalse(out["changed"])
        self.assertEqual(len(out["klines"]), 2)
        self.assertEqual(out["klines"][-1]["date"], "2026-09-11")

    def test_stale_bar_older_than_last_ignored(self):
        klines = [{"date": "2026-09-11", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000}]
        out = self._run_apply(klines, [{"date": "2026-09-10", "close": 99}])
        self.assertFalse(out["changed"])
        self.assertEqual(len(out["klines"]), 1)
        self.assertEqual(out["klines"][-1]["close"], 10.5)

    def test_empty_or_invalid_bars_noop(self):
        klines = [{"date": "2026-09-11", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000}]
        self.assertFalse(self._run_apply(klines, [])["changed"])
        self.assertFalse(self._run_apply(klines, [{"open": 1}])["changed"])
        self.assertFalse(self._run_apply(klines, None)["changed"])

    def test_reject_minute_period(self):
        klines = [{"date": "2026-09-11", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000}]
        out = self._run_apply(klines, [{"date": "2026-09-14", "close": 11}], period="5m")
        self.assertFalse(out["changed"])

    def test_reconcile_appends_newer_tail_bar_when_revision_loses(self):
        # 整图 revision 更新但不含当日 bar; 更早到达的 tail 含当日 bar, 仍要补上。
        js = (
            "const STATE = {symbol:'S', period:'1d', adjust:'forward', klineData:{"
            " symbol:'S', period:'1d', klines:[{date:'2026-09-29', close:10, ma5:1}]}};\n"
            + "const tail = {symbol:'S', period:'1d', meta:{adjust:'forward'}, _revision:1, bars:["
            " {date:'2026-09-29', close:10, ma5:1},"
            " {date:'2026-09-30', close:11, ma5:2, macd_dif:0.3}]};\n"
            + "const liveQuotes = {stored: tail, get(){return this.stored;},"
            " accept(kind, symbol, payload){"
            "  const prev = Number(this.stored && this.stored._revision)||0;"
            "  const rev = Number(payload._revision)||0;"
            "  if (prev && rev && rev < prev) return false;"
            "  this.stored = payload; return true;}};\n"
            + "function ensureQuoteStream(){}\n"
            + self._extract("applyServerBars") + "\n"
            + self._extract("reconcileLiveBars") + "\n"
            + "const data = {symbol:'S', period:'1d', meta:{adjust:'forward'}, _revision:5,"
            " klines:[{date:'2026-09-29', close:10, ma5:1}]};\n"
            + "reconcileLiveBars(data);\n"
            + "const last = STATE.klineData.klines[STATE.klineData.klines.length-1];\n"
            + "process.stdout.write(JSON.stringify({n: STATE.klineData.klines.length, last}));"
        )
        out = json.loads(run_node(js))
        self.assertEqual(out["n"], 2)
        self.assertEqual(out["last"]["date"], "2026-09-30")
        self.assertEqual(out["last"]["ma5"], 2)
        self.assertEqual(out["last"]["close"], 11)

    def _run_patch(self, state, quote):
        js = (
            "const STATE = " + json.dumps(state) + ";\n"
            + "const draws = [];\n"
            + "function requestBarPatchDraw(){ draws.push(1); }\n"
            + self._extract("patchTodayBarOhlcv") + "\n"
            + "const q = JSON.parse(process.argv[1]);\n"
            + "const changed = patchTodayBarOhlcv(q, 'S');\n"
            + "const last = STATE.klineData.klines[STATE.klineData.klines.length-1];\n"
            + "process.stdout.write(JSON.stringify({changed, draws: draws.length, last,"
            " n: STATE.klineData.klines.length}));"
        )
        return json.loads(run_node(js, json.dumps(quote)))

    def test_quote_updates_same_day_ohlcv_only(self):
        # 2026-09-30 02:00 UTC = 北京时间 10:00, 与末根同日
        import calendar
        from datetime import datetime, timezone
        ts = calendar.timegm(datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc).utctimetuple())
        state = {
            "symbol": "S", "period": "1d",
            "klineData": {
                "symbol": "S",
                "klines": [{"date": "2026-09-29", "open": 1, "high": 2, "low": 0.5, "close": 1.5,
                            "volume": 10, "amount": 20, "ma5": 9, "macd_dif": 0.4},
                           {"date": "2026-09-30", "open": 10, "high": 11, "low": 9, "close": 10.5,
                            "volume": 1000, "amount": 10000, "ma5": 8.8, "macd_dif": 0.2}],
            },
        }
        quote = {"timestamp": ts, "open": 10.2, "high": 12, "low": 9.5,
                 "last_price": 11.7, "volume": 3000, "amount": 35000}
        out = self._run_patch(state, quote)
        self.assertTrue(out["changed"])
        self.assertEqual(out["draws"], 1)
        self.assertEqual(out["n"], 2, "不得新增 bar")
        last = out["last"]
        self.assertEqual(last["close"], 11.7)
        self.assertEqual(last["high"], 12)
        self.assertEqual(last["volume"], 3000)
        self.assertEqual(last["ma5"], 8.8, "指标不随快照改")
        self.assertEqual(last["macd_dif"], 0.2)
        self.assertEqual(out["last"]["date"], "2026-09-30")

    def test_quote_does_not_create_or_touch_other_bars(self):
        import calendar
        from datetime import datetime, timezone
        ts = calendar.timegm(datetime(2026, 9, 30, 2, 0, tzinfo=timezone.utc).utctimetuple())
        bar = {"date": "2026-09-29", "open": 10, "high": 11, "low": 9, "close": 10.5,
               "volume": 1000, "ma5": 8}
        state = {"symbol": "S", "period": "1d", "klineData": {"symbol": "S", "klines": [dict(bar)]}}
        quote = {"timestamp": ts, "open": 1, "high": 2, "low": 0.5, "last_price": 1.5, "volume": 9}
        out = self._run_patch(state, quote)
        self.assertFalse(out["changed"], "末根不是快照那天, 不更新也不新增")
        self.assertEqual(out["n"], 1)
        self.assertEqual(out["last"]["close"], 10.5)

        state["period"] = "5m"
        state["klineData"]["klines"][0]["date"] = "2026-09-30"
        out = self._run_patch(state, quote)
        self.assertFalse(out["changed"], "非日K不动")

        state["period"] = "1d"
        quote["volume"] = 0
        out = self._run_patch(state, quote)
        self.assertFalse(out["changed"], "volume=0 不动")

        quote = {"open": 1, "high": 2, "low": 0.5, "last_price": 1.5, "volume": 9}
        state["klineData"]["meta"] = {"is_trading_day": False, "server_time": "2026-09-30 10:00:00"}
        out = self._run_patch(state, quote)
        self.assertFalse(out["changed"], "无时间戳且非交易日, 不用本地日期猜")

        state["klineData"]["meta"]["is_trading_day"] = True
        out = self._run_patch(state, quote)
        self.assertTrue(out["changed"], "无时间戳时用服务端交易日 + server_time")
        self.assertEqual(out["last"]["close"], 1.5)
        self.assertEqual(out["last"]["ma5"], 8)


if __name__ == "__main__":
    unittest.main()
