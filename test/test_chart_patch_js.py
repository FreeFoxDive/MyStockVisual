# -*- coding: utf-8 -*-
"""图表 patch 纯逻辑镜像测试（无 ECharts）。"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest

_CHART_PATCH_JS = r"""
const input = JSON.parse(process.argv[1]);

function buildMacdHistPoint(k, colors) {
  return {
    value: k.macd_hist,
    itemStyle: { color: (k.macd_hist || 0) >= 0 ? colors.up : colors.down },
  };
}

function appendIndicatorSeriesPatches(seriesPatch, klines, panels) {
  panels.forEach((p) => {
    if (p === 'macd') {
      seriesPatch.push(
        { name: 'DIF', data: klines.map((k) => k.macd_dif) },
        { name: 'DEA', data: klines.map((k) => k.macd_dea) },
        { name: 'MACD柱', data: klines.map((k) => buildMacdHistPoint(k, input.colors)) },
      );
    } else if (p === 'kdj') {
      seriesPatch.push(
        { name: 'K', data: klines.map((k) => k.kdj_k) },
        { name: 'D', data: klines.map((k) => k.kdj_d) },
        { name: 'J', data: klines.map((k) => k.kdj_j) },
      );
    } else if (p === 'rsi') {
      seriesPatch.push(
        { name: 'RSI1', data: klines.map((k) => k.rsi6) },
        { name: 'RSI2', data: klines.map((k) => k.rsi12) },
        { name: 'RSI3', data: klines.map((k) => k.rsi24) },
      );
    } else if (p === 'atr') {
      seriesPatch.push({ name: 'ATR(14)', data: klines.map((k) => k.atr14) });
    }
  });
}

function patchIndicatorLastPoints(seriesPatch, k, lastIdx, seriesByName, klines, panels, channelEnabled) {
  const copyLast = (name, point) => {
    const s = seriesByName[name];
    if (!s || !s.data || s.data.length !== klines.length) return;
    const arr = s.data.slice();
    arr[lastIdx] = point;
    seriesPatch.push({ name, data: arr });
  };
  if (panels.includes('macd')) {
    copyLast('DIF', k.macd_dif);
    copyLast('DEA', k.macd_dea);
    copyLast('MACD柱', buildMacdHistPoint(k, input.colors));
  }
  if (panels.includes('rsi')) {
    copyLast('RSI1', k.rsi6);
    copyLast('RSI2', k.rsi12);
    copyLast('RSI3', k.rsi24);
  }
  if (channelEnabled && k.ema13 != null && k.atr14 != null) {
  [1, 2, 3].forEach((mult) => {
      copyLast(`+${mult}ATR`, k.ema13 + mult * k.atr14);
      copyLast(`-${mult}ATR`, k.ema13 - mult * k.atr14);
    });
  }
}

const { mode, klines, panels, k, seriesByName, channelEnabled } = input;
const colors = input.colors;
let result;
if (mode === 'hist') {
  result = buildMacdHistPoint(k, colors);
} else if (mode === 'append') {
  const sp = [];
  appendIndicatorSeriesPatches(sp, klines, panels);
  result = sp;
} else if (mode === 'patchLast') {
  const sp = [];
  patchIndicatorLastPoints(sp, k, klines.length - 1, seriesByName, klines, panels, channelEnabled);
  result = sp;
}
process.stdout.write(JSON.stringify(result));
"""


def _run_chart_patch(payload: dict):
    proc = subprocess.run(
        ["node", "-e", _CHART_PATCH_JS, json.dumps(payload)],
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
class TestChartPatchJs(unittest.TestCase):
    COLORS = {"up": "#ef5350", "down": "#26a69a"}

    def test_build_macd_hist_positive_color(self):
        out = _run_chart_patch({
            "mode": "hist",
            "colors": self.COLORS,
            "k": {"macd_hist": 0.5},
        })
        self.assertEqual(out["value"], 0.5)
        self.assertEqual(out["itemStyle"]["color"], self.COLORS["up"])

    def test_build_macd_hist_negative_color(self):
        out = _run_chart_patch({
            "mode": "hist",
            "colors": self.COLORS,
            "k": {"macd_hist": -0.3},
        })
        self.assertEqual(out["itemStyle"]["color"], self.COLORS["down"])

    def test_append_indicator_series_macd_kdj(self):
        klines = [
            {"macd_dif": 1, "macd_dea": 0.5, "macd_hist": 0.2, "kdj_k": 50, "kdj_d": 48, "kdj_j": 54},
            {"macd_dif": 1.1, "macd_dea": 0.6, "macd_hist": -0.1, "kdj_k": 51, "kdj_d": 49, "kdj_j": 55},
        ]
        out = _run_chart_patch({
            "mode": "append",
            "colors": self.COLORS,
            "klines": klines,
            "panels": ["macd", "kdj"],
        })
        names = [s["name"] for s in out]
        self.assertIn("DIF", names)
        self.assertIn("MACD柱", names)
        self.assertIn("K", names)
        dif = next(s for s in out if s["name"] == "DIF")
        self.assertEqual(len(dif["data"]), 2)

    def test_patch_indicator_last_points(self):
        klines = [
            {"macd_dif": 1, "macd_dea": 0.5, "macd_hist": 0.2, "rsi6": 40, "rsi12": 45, "rsi24": 50, "ema13": 10, "atr14": 0.5},
            {"macd_dif": 1.2, "macd_dea": 0.6, "macd_hist": -0.1, "rsi6": 42, "rsi12": 46, "rsi24": 51, "ema13": 10.2, "atr14": 0.6},
        ]
        series_by_name = {
            "DIF": {"data": [1, 1.1]},
            "DEA": {"data": [0.5, 0.55]},
            "MACD柱": {"data": [{"value": 0.2}, {"value": 0.1}]},
            "RSI1": {"data": [40, 41]},
            "+1ATR": {"data": [10.5, 10.7]},
        }
        out = _run_chart_patch({
            "mode": "patchLast",
            "colors": self.COLORS,
            "klines": klines,
            "k": klines[-1],
            "panels": ["macd", "rsi"],
            "seriesByName": series_by_name,
            "channelEnabled": True,
        })
        names = {s["name"] for s in out}
        self.assertIn("DIF", names)
        self.assertIn("RSI1", names)
        self.assertIn("+1ATR", names)
        dif = next(s for s in out if s["name"] == "DIF")
        self.assertEqual(dif["data"][-1], 1.2)


if __name__ == "__main__":
    unittest.main()
