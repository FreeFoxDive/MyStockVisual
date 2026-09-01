# -*- coding: utf-8 -*-
"""Node + indicators.js 测试公共工具。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest

import numpy as np
import pandas as pd

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
VISUAL_DIR = os.path.dirname(TEST_DIR)
PROJECT_DIR = os.path.dirname(VISUAL_DIR)
INDICATORS_JS = os.path.join(VISUAL_DIR, "static", "js", "indicators.js")

if VISUAL_DIR not in sys.path:
    sys.path.insert(0, VISUAL_DIR)
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from indicators import compute_all_indicators

INDICATOR_KEYS = [
    "ma5", "ma10", "ma20",
    "macd_dif", "macd_dea", "macd_hist",
    "ema13", "kdj_k", "kdj_d", "kdj_j",
    "rsi6", "rsi12", "rsi24", "atr14",
]

NODE_AVAILABLE = shutil.which("node") is not None


def require_node():
    if not NODE_AVAILABLE:
        raise unittest.SkipTest("需要 node 才能跑前端镜像测试")
    if not os.path.isfile(INDICATORS_JS):
        raise unittest.SkipTest("indicators.js missing")


def run_node(script: str, *args: str) -> str:
    """执行 node -e script，额外 argv 传入 process.argv。"""
    require_node()
    proc = subprocess.run(
        ["node", "-e", script, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or f"node exit {proc.returncode}")
    return proc.stdout.strip()


def run_indicators_js(body: str, *args: str) -> str:
    """require indicators.js 后执行 body（可用 IndicatorCalc）。"""
    js_path = INDICATORS_JS.replace("\\", "/")
    script = f"""
const IndicatorCalc = require({json.dumps(js_path)});
{body}
"""
    return run_node(script, *args)


def recalc_tail_js(klines: list, period: str = "1d") -> dict:
    """调用 recalcTailIndicators，返回末根 bar dict。"""
    out = run_indicators_js(
        "const klines = JSON.parse(process.argv[1]);"
        "const period = process.argv[2];"
        "IndicatorCalc.recalcTailIndicators(klines, period);"
        "process.stdout.write(JSON.stringify(klines[klines.length - 1]));",
        json.dumps(klines),
        period,
    )
    return json.loads(out)


def make_kline_fixture(n: int = 80, seed: int = 42) -> list:
    rng = np.random.default_rng(seed)
    base = 100.0
    rows = []
    start = pd.Timestamp("2024-01-02")
    for i in range(n):
        chg = rng.normal(0, 1.2)
        o = base
        c = max(1.0, base + chg)
        h = max(o, c) + abs(rng.normal(0, 0.5))
        l = min(o, c) - abs(rng.normal(0, 0.5))
        v = int(rng.integers(1_000_000, 5_000_000))
        dt = (start + pd.Timedelta(days=i)).strftime("%Y-%m-%d")
        rows.append({
            "date": dt,
            "open": float(o),
            "high": float(h),
            "low": float(l),
            "close": float(c),
            "volume": v,
            "amount": float(v * c),
        })
        base = c
    return rows


def python_last_indicators(klines: list, tail: int = 60, period: str = "1d") -> dict:
    slice_k = klines[-tail:] if len(klines) > tail else klines
    df = pd.DataFrame(slice_k)
    df = df.set_index(pd.to_datetime(df["date"]))
    out, _ = compute_all_indicators(df, period=period)
    row = out.iloc[-1]
    return {
        k: (None if pd.isna(row[k]) else float(row[k]))
        for k in INDICATOR_KEYS
    }


def assert_js_py_parity(test_case, klines: list, period: str = "1d", places: int = 4):
    """断言 JS recalcTailIndicators 末根与 Python 一致。"""
    require_node()
    import copy
    k_copy = copy.deepcopy(klines)
    py = python_last_indicators(k_copy, period=period)
    js = recalc_tail_js(k_copy, period=period)
    for key in INDICATOR_KEYS:
        if py[key] is None and js.get(key) is None:
            continue
        test_case.assertIsNotNone(js.get(key), msg=key)
        test_case.assertAlmostEqual(js[key], py[key], places=places, msg=key)
