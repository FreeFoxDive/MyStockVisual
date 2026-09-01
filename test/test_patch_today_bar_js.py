# -*- coding: utf-8 -*-
"""patchTodayBarFromQuote 逻辑镜像测试（与 index.html 一致）。"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest

_PATCH_JS = r"""
const path = require('path');
const IndicatorCalc = require(path.join(process.argv[1], 'static', 'js', 'indicators.js'));

function patchTodayBarFromQuote(state, q, todayStr) {
  if (state.period !== '1d' || !state.klineData || !state.klineData.klines || !state.klineData.klines.length) return false;
  if (!q || q.last_price == null || !q.volume || q.volume <= 0) return false;
  if (q.high == null || q.low == null) return false;
  const klines = state.klineData.klines;
  const last = klines[klines.length - 1];
  const lastDate = String(last.date).slice(0, 10);
  const next = {
    open: q.open != null ? q.open : last.open,
    high: q.high,
    low: q.low,
    close: q.last_price,
    volume: q.volume,
  };
  if (lastDate < todayStr) {
    klines.push({ date: todayStr, ...next, amount: q.amount });
    IndicatorCalc.recalcTailIndicators(klines, '1d');
    return true;
  }
  if (lastDate !== todayStr) return false;
  const changed = last.open !== next.open || last.high !== next.high || last.low !== next.low
    || last.close !== next.close || last.volume !== next.volume;
  if (!changed) return false;
  Object.assign(last, next);
  IndicatorCalc.recalcTailIndicators(klines, '1d');
  return true;
}

const visualDir = process.argv[1];
const input = JSON.parse(process.argv[2]);
const out = patchTodayBarFromQuote(input.state, input.quote, input.today);
process.stdout.write(JSON.stringify({
  ok: out,
  klines: input.state.klineData.klines,
}));
"""


def _run_patch(visual_dir: str, state: dict, quote: dict, today: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", _PATCH_JS, visual_dir, json.dumps({"state": state, "quote": quote, "today": today})],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    return json.loads(proc.stdout)


@unittest.skipUnless(shutil.which("node"), "需要 node")
class TestPatchTodayBarJs(unittest.TestCase):
    VISUAL_DIR = __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__)))

    def _base_state(self, klines, period="1d"):
        return {"period": period, "klineData": {"klines": [dict(k) for k in klines]}}

    def test_append_when_history_ends_yesterday(self):
        klines = [
            {"date": "2026-08-27", "open": 10, "high": 11, "low": 9, "close": 10.0, "volume": 1000},
            {"date": "2026-08-28", "open": 10.1, "high": 11.1, "low": 9.1, "close": 10.2, "volume": 1100},
            {"date": "2026-08-29", "open": 10.2, "high": 11.2, "low": 9.2, "close": 10.4, "volume": 1200},
            {"date": "2026-08-30", "open": 10.3, "high": 11.3, "low": 9.3, "close": 10.6, "volume": 1300},
            {"date": "2026-08-31", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000},
        ]
        state = self._base_state(klines)
        quote = {"last_price": 11.0, "open": 10.8, "high": 11.2, "low": 10.7, "volume": 2000, "amount": 22000}
        out = _run_patch(self.VISUAL_DIR, state, quote, "2026-09-01")
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["klines"]), 6)
        last = out["klines"][-1]
        self.assertEqual(last["date"], "2026-09-01")
        self.assertEqual(last["close"], 11.0)
        self.assertIsNotNone(last.get("ma5"))

    def test_update_today_bar_when_changed(self):
        state = self._base_state([{"date": "2026-09-01", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000}])
        quote = {"last_price": 10.8, "open": 10, "high": 11, "low": 9, "volume": 1500, "amount": 16200}
        out = _run_patch(self.VISUAL_DIR, state, quote, "2026-09-01")
        self.assertTrue(out["ok"])
        self.assertEqual(out["klines"][-1]["close"], 10.8)

    def test_no_change_returns_false(self):
        state = self._base_state([{"date": "2026-09-01", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000}])
        quote = {"last_price": 10.5, "open": 10, "high": 11, "low": 9, "volume": 1000}
        out = _run_patch(self.VISUAL_DIR, state, quote, "2026-09-01")
        self.assertFalse(out["ok"])

    def test_reject_non_1d(self):
        state = self._base_state([{"date": "2026-09-01", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000}], period="1w")
        quote = {"last_price": 11, "high": 11, "low": 9, "volume": 100}
        out = _run_patch(self.VISUAL_DIR, state, quote, "2026-09-01")
        self.assertFalse(out["ok"])

    def test_reject_zero_volume(self):
        state = self._base_state([{"date": "2026-09-01", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000}])
        quote = {"last_price": 11, "high": 11, "low": 9, "volume": 0}
        out = _run_patch(self.VISUAL_DIR, state, quote, "2026-09-01")
        self.assertFalse(out["ok"])

    def test_reject_missing_high(self):
        state = self._base_state([{"date": "2026-09-01", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000}])
        quote = {"last_price": 11, "low": 9, "volume": 100}
        out = _run_patch(self.VISUAL_DIR, state, quote, "2026-09-01")
        self.assertFalse(out["ok"])

    def test_history_after_today_returns_false(self):
        state = self._base_state([{"date": "2026-09-02", "open": 10, "high": 11, "low": 9, "close": 10.5, "volume": 1000}])
        quote = {"last_price": 11, "open": 10, "high": 11, "low": 9, "volume": 1000}
        out = _run_patch(self.VISUAL_DIR, state, quote, "2026-09-01")
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
