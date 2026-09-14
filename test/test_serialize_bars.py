# -*- coding: utf-8 -*-
"""K线序列化向量化: serialize_bars 与逐行 iterrows 逐字段等价。

df.iterrows() 每行构造一个 Series (实测 1006 根约 86ms, 是指标计算的 3 倍),
故改为按列 tolist() 后拼 dict。这里锁定等价性 —— 口径/字段/顺序都不得变,
否则前端某条指标曲线会静默消失或漂移。

运行:
    venv/Scripts/python.exe -u visual/test/test_serialize_bars.py
"""
from __future__ import annotations

import os
import json
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

from api.kline import _BAR_FIELDS, serialize_bars, _safe_float, _safe_int  # noqa: E402

# 契约字段清单: 与重构前的 _serialize_bar 完全一致 (顺序无关, 集合必须相等)
_EXPECTED_ORDER = (
    "date", "open", "high", "low", "close", "volume", "amount",
    "ma5", "ma10", "ma20",
    "macd_dif", "macd_dea", "macd_hist",
    "rsi6", "rsi12", "rsi24",
    "kdj_k", "kdj_d", "kdj_j", "atr14", "ema13", "impulse",
    "obv", "maobv", "vol_ma5", "vol_ma10", "vol_ma20",
    "boll_mid", "boll_up", "boll_low", "wr14", "cci14",
    "bias6", "bias12", "bias24", "dmi_pdi", "dmi_mdi", "dmi_adx",
)
_EXPECTED_FIELDS = set(_EXPECTED_ORDER)


def _legacy_bars(df, period):
    """重构前的实现 (逐行 iterrows), 仅作等价性对照。"""
    out = []
    for idx, row in df.iterrows():
        date = idx.strftime('%Y-%m-%d %H:%M' if period in ('1m', '5m', '15m', '30m', '60m')
                            else '%Y-%m-%d') if hasattr(idx, 'strftime') else str(idx)
        bar = {'date': date}
        for name in _EXPECTED_ORDER[1:]:
            convert = _safe_int if name in ('volume', 'impulse') else _safe_float
            bar[name] = convert(row.get(name))
        out.append(bar)
    return out


def _rich_df(n=40, minute=False):
    """含 NaN warmup 段/整列缺失/整数列的 df, 覆盖转换器全部分支。"""
    idx = (pd.date_range("2026-01-01 09:30", periods=n, freq="5min") if minute
           else pd.date_range("2026-01-01", periods=n, freq="D"))
    base = pd.Series(np.arange(n, dtype=float), index=idx)
    df = pd.DataFrame({
        "open": 10.0 + base * 0.01,
        "high": 10.2 + base * 0.01,
        "low": 9.8 + base * 0.01,
        "close": 10.1 + base * 0.01,
        "volume": (base + 1000).astype("int64"),
        "amount": 1_000_000.0 + base,
        "ma5": 10.0 + base * 0.01,
        "impulse": base.astype("int64") % 3 - 1,
        "obv": base * 100,
        "maobv": base * 90,
        "vol_ma5": base + 900,
        "boll_mid": 10.0, "boll_up": 10.5, "boll_low": 9.5,
        "wr14": -50.0, "cci14": 0.0,
        "bias6": 0.1, "bias12": 0.2, "bias24": 0.3,
        "dmi_pdi": 20.0, "dmi_mdi": 15.0, "dmi_adx": 18.0,
        "macd_dif": 0.01, "macd_dea": 0.02, "macd_hist": -0.01,
        "rsi6": 55.0, "rsi12": 52.0, "rsi24": 50.0,
        "kdj_k": 60.0, "kdj_d": 58.0, "kdj_j": 64.0,
        "atr14": 0.25, "ema13": 10.05,
    }, index=idx)
    # warmup: 前 4 根指标缺失 (NaN → null)
    for col in ("ma5", "obv", "maobv", "vol_ma5", "macd_dif", "ema13"):
        df.loc[df.index[:4], col] = np.nan
    # ma10/ma20 故意不提供: 覆盖「整列缺失 → null, 不得凭空造数」的路径
    assert "ma10" not in df.columns and "ma20" not in df.columns
    return df


class SerializeBarsEquivalence(unittest.TestCase):
    def _assert_same(self, df, period):
        got = serialize_bars(df, period)
        want = _legacy_bars(df, period)
        self.assertEqual(json.dumps(got), json.dumps(want), 'JSON 字节/字段顺序不同')
        self.assertEqual(len(got), len(want))
        for i, (g, w) in enumerate(zip(got, want)):
            self.assertEqual(set(g.keys()), set(w.keys()),
                             f"第 {i} 根字段集合不同")
            for k in w:
                self.assertEqual(g[k], w[k],
                                 f"第 {i} 根字段 {k}: {g[k]!r} != {w[k]!r}")

    def test_equivalent_on_daily_fixture(self):
        self._assert_same(_rich_df(), "1d")

    def test_equivalent_on_minute_fixture(self):
        """分钟周期日期格式到分钟 (%Y-%m-%d %H:%M)。"""
        self._assert_same(_rich_df(minute=True), "5m")

    def test_equivalent_on_weekly_fixture(self):
        self._assert_same(_rich_df(), "1w")

    def test_field_set_matches_contract(self):
        got = serialize_bars(_rich_df(), "1d")
        self.assertEqual(set(got[0].keys()), _EXPECTED_FIELDS)
        self.assertEqual([n for n, _ in _BAR_FIELDS],
                         list(_EXPECTED_ORDER[1:]), "字段表需保持稳定顺序")
        self.assertEqual({n for n, _ in _BAR_FIELDS} | {"date"}, _EXPECTED_FIELDS)

    def test_missing_columns_become_null_not_zero(self):
        df = _rich_df()
        bar = serialize_bars(df, "1d")[0]
        self.assertIsNone(bar["ma10"], "整列缺失必须是 null, 不能补 0")
        self.assertIsNone(bar["ma20"])

    def test_warmup_nan_becomes_null(self):
        bar = serialize_bars(_rich_df(), "1d")[0]
        self.assertIsNone(bar["ma5"])
        self.assertIsNotNone(bar["boll_mid"])

    def test_volume_stays_int(self):
        bar = serialize_bars(_rich_df(), "1d")[0]
        self.assertIsInstance(bar["volume"], int)
        self.assertIsInstance(bar["impulse"], int)

    def test_empty_df_returns_empty_list(self):
        self.assertEqual(serialize_bars(_rich_df().iloc[:0], "1d"), [])

    def test_non_datetime_index_falls_back_to_str(self):
        df = _rich_df()
        df.index = [f"row{i}" for i in range(len(df))]
        self._assert_same(df, "1d")

    def test_equivalent_on_real_cached_payload(self):
        """真实缓存载荷 (若存在): 1006 根全字段逐一比对。"""
        import glob
        import gzip
        import json
        import market
        files = sorted(glob.glob(
            str(_VISUAL_DIR / ".cache" / "klines" / "*_1d_1006_qfq_*.json.gz")))
        if not files:
            self.skipTest("无本地缓存样本")
        with gzip.open(files[-1], "rt", encoding="utf-8") as f:
            payload = json.load(f)
        df = market._normalize(pd.DataFrame(payload["data"]), prefer_time=False)
        self.assertIsNotNone(df)
        self.assertGreater(len(df), 100)
        self._assert_same(df, "1d")


if __name__ == "__main__":
    unittest.main()
