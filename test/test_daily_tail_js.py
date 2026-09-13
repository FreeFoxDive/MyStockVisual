# -*- coding: utf-8 -*-
"""日K末根增量: applyServerBars 从 static/index.html 抽取真实函数测试。

契约: 前端只应用后端下发的 bar (按 date 合并/追加), 绝不由快照派生 OHLCV。
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
        # 前端不得再从快照拼 bar, 也不得再本地重算指标
        for gone in ("patchTodayBarFromQuote", "recalcTailIndicators", "todayYMD"):
            self.assertNotIn(gone, self.src, f"index.html 仍含已废弃的 {gone}")
        self.assertIn("/api/kline/tail", self.src, "index.html 未接后端 tail 接口")

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

    def test_reject_non_1d(self):
        klines = [{"date": "2026-09-11", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000}]
        out = self._run_apply(klines, [{"date": "2026-09-14", "close": 11}], period="1w")
        self.assertFalse(out["changed"])


if __name__ == "__main__":
    unittest.main()
