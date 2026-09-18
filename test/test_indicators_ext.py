# -*- coding: utf-8 -*-
"""新增指标 BOLL / WR / CCI / BIAS / DMI 数值与输出契约测试 (纯函数, 零 mock)。

现有 JS 单元测试与 Python↔JS 对照均未覆盖这 5 个指标, 这里补数值与 warmup 断言。

运行:
    venv/Scripts/python.exe -u visual/test/test_indicators_ext.py
"""

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

from indicators import (  # noqa: E402
    boll, wr, cci, bias, dmi, compute_all_indicators,
    volume_shares_mult, intraday_avg_price,
)


def _ohlcv(n=60):
    close = pd.Series([10.0 + 0.2 * i + (1.0 if i % 3 == 0 else 0.0) for i in range(n)])
    return pd.DataFrame({
        "open": close - 0.1, "high": close + 0.5, "low": close - 0.5,
        "close": close, "volume": [1000.0 + 10 * i for i in range(n)],
    })


class BollTest(unittest.TestCase):
    def test_matches_sample_std_formula(self):
        close = pd.Series([float(i) for i in range(1, 31)])
        mid, up, low = boll(close, 20, 2)
        exp_mid = close.rolling(20, min_periods=20).mean()
        exp_std = close.rolling(20, min_periods=20).std(ddof=1)
        self.assertAlmostEqual(mid.iloc[-1], exp_mid.iloc[-1], places=9)
        self.assertAlmostEqual(up.iloc[-1], (exp_mid + 2 * exp_std).iloc[-1], places=9)
        self.assertAlmostEqual(low.iloc[-1], (exp_mid - 2 * exp_std).iloc[-1], places=9)
        # 样本标准差 (ddof=1), 不是总体标准差
        self.assertNotAlmostEqual(float(exp_std.iloc[-1]), float(close.rolling(20).std(ddof=0).iloc[-1]), places=6)

    def test_warmup_nan(self):
        mid, up, low = boll(pd.Series([float(i) for i in range(1, 31)]), 20, 2)
        self.assertTrue(np.isnan(mid.iloc[18]))
        self.assertFalse(np.isnan(mid.iloc[19]))


class WrTest(unittest.TestCase):
    def _series(self, close_val):
        n = 20
        return (pd.Series([10.0] * n), pd.Series([8.0] * n), pd.Series([close_val] * n))

    def test_at_high_is_zero_at_low_is_hundred(self):
        h, l, _ = self._series(10.0)
        self.assertAlmostEqual(wr(h, l, pd.Series([10.0] * 20), 14).iloc[-1], 0.0, places=9)
        self.assertAlmostEqual(wr(h, l, pd.Series([8.0] * 20), 14).iloc[-1], 100.0, places=9)

    def test_zero_range_is_nan(self):
        flat = pd.Series([10.0] * 20)
        self.assertTrue(np.isnan(wr(flat, flat, flat, 14).iloc[-1]))

    def test_warmup_nan(self):
        h, l, c = self._series(9.0)
        out = wr(h, l, c, 14)
        self.assertTrue(np.isnan(out.iloc[12]))
        self.assertFalse(np.isnan(out.iloc[13]))


class CciTest(unittest.TestCase):
    def test_flat_series_md_zero_is_nan(self):
        flat = pd.Series([10.0] * 20)
        self.assertTrue(np.isnan(cci(flat, flat, flat, 14).iloc[-1]))

    def test_matches_formula(self):
        df = _ohlcv(30)
        out = cci(df["high"], df["low"], df["close"], 14)
        tp = (df["high"] + df["low"] + df["close"]) / 3
        ma = tp.rolling(14, min_periods=14).mean()
        md = (tp - ma).abs().rolling(14, min_periods=14).mean()
        exp = (tp - ma) / (0.015 * md)
        self.assertAlmostEqual(out.iloc[-1], exp.iloc[-1], places=6)


class BiasTest(unittest.TestCase):
    def test_formula_and_keys(self):
        close = pd.Series([10.0 + 0.5 * i for i in range(30)])
        out = bias(close, (6, 12, 24))
        self.assertEqual(set(out), {"bias6", "bias12", "bias24"})
        for n in (6, 12, 24):
            exp = (close - close.rolling(n, min_periods=n).mean()) / close.rolling(n, min_periods=n).mean() * 100
            self.assertAlmostEqual(out[f"bias{n}"].iloc[-1], exp.iloc[-1], places=9)
        # 单调上涨 → 正乖离
        self.assertGreater(out["bias6"].iloc[-1], 0)


class DmiTest(unittest.TestCase):
    def test_monotonic_rise_gives_zero_mdi_and_adx_100(self):
        n = 40
        close = pd.Series([10.0 + i for i in range(n)])
        h, l = close + 1, close - 1
        pdi, mdi, adx = dmi(h, l, close, 14, 6)
        self.assertGreater(pdi.iloc[-1], 0)
        self.assertAlmostEqual(mdi.iloc[-1], 0.0, places=9)
        self.assertAlmostEqual(adx.iloc[-1], 100.0, places=6, msg="单边上涨 DX=100, ADX 收敛到 100")

    def test_warmup(self):
        n = 40
        close = pd.Series([10.0 + i for i in range(n)])
        pdi, mdi, adx = dmi(close + 1, close - 1, close, 14, 6)
        self.assertTrue(np.isnan(pdi.iloc[12]) and np.isnan(mdi.iloc[12]))
        self.assertFalse(np.isnan(pdi.iloc[13]))
        self.assertTrue(np.isnan(adx.iloc[17]), "ADX 需在 DX 之后 6 期")
        self.assertFalse(np.isnan(adx.iloc[18]))


class ComputeAllContractTest(unittest.TestCase):
    def setUp(self):
        self.df = _ohlcv(60)
        self.result, self.ind = compute_all_indicators(self.df, period="1d")

    def test_indicator_dict_shape(self):
        self.assertEqual(self.ind["boll"]["params"], {"n": 20, "k": 2})
        self.assertEqual(self.ind["wr"]["params"], {"period": 14})
        self.assertEqual(self.ind["cci"]["params"], {"period": 14})
        self.assertEqual(self.ind["bias"]["params"], {"periods": [6, 12, 24]})
        self.assertEqual(self.ind["dmi"]["params"], {"period": 14, "ma": 6})
        for key in ("mid", "up", "low"):
            self.assertEqual(len(self.ind["boll"][key]), len(self.df))
        for key in ("bias6", "bias12", "bias24"):
            self.assertEqual(len(self.ind["bias"][key]), len(self.df))
        for key in ("pdi", "mdi", "adx"):
            self.assertEqual(len(self.ind["dmi"][key]), len(self.df))

    def test_series_columns_present(self):
        for col in ("boll_mid", "boll_up", "boll_low", "wr14", "cci14",
                    "bias6", "bias12", "bias24", "dmi_pdi", "dmi_mdi", "dmi_adx"):
            self.assertIn(col, self.result.columns)

    def test_nan_becomes_none(self):
        # warmup 段应为 None 而非 NaN (JSON 友好)
        self.assertIsNone(self.ind["boll"]["mid"][0])
        self.assertIsNone(self.ind["dmi"]["adx"][0])
        self.assertIsNotNone(self.ind["boll"]["mid"][-1])

    def test_values_match_standalone_functions(self):
        c, h, l = self.df["close"], self.df["high"], self.df["low"]
        self.assertAlmostEqual(self.ind["wr"]["values"][-1], float(wr(h, l, c, 14).iloc[-1]), places=6)
        self.assertAlmostEqual(self.ind["cci"]["values"][-1], float(cci(h, l, c, 14).iloc[-1]), places=6)
        pdi, mdi, adx = dmi(h, l, c, 14, 6)
        self.assertAlmostEqual(self.ind["dmi"]["pdi"][-1], float(pdi.iloc[-1]), places=6)
        self.assertAlmostEqual(self.ind["dmi"]["adx"][-1], float(adx.iloc[-1]), places=6)


class VolumeSharesMultTest(unittest.TestCase):
    """成交量单位系数: 只按量级取最近的手数档 (1/10/100)。判错会让均价整体差一个量级。"""

    def test_lots_shares_and_convertible_bond(self):
        # amount = close × volume × 手数: 比值就是手数, 与价格无关
        for lot, want in ((100, 100), (10, 10), (1, 1)):
            close = [10.0, 20.0, 8.0]
            vol = [1000.0, 500.0, 800.0]
            amount = [c * v * lot for c, v in zip(close, vol)]
            self.assertEqual(volume_shares_mult(close, vol, amount, tail=None), want,
                             f"1手={lot} 的标的应识别为系数 {want}")

    def test_forward_adjusted_price_does_not_flip_factor(self):
        # 前复权价配未复权成交额: 比值按复权系数整体缩放, 量级档不能变
        close = [10.0, 10.5, 11.0]
        vol = [100.0, 200.0, 150.0]
        amount = [c * v * 100 * 1.37 for c, v in zip(close, vol)]
        self.assertEqual(volume_shares_mult(close, vol, amount, tail=None), 100)

    def test_tail_window_and_fallback(self):
        # 只有最后 tail 根参与: 前面的脏数据不影响判定
        close = [1.0] * 20
        vol = [100.0] * 20
        amount = [1.0] * 10 + [c * v * 100 for c, v in zip(close[10:], vol[10:])]
        self.assertEqual(volume_shares_mult(close, vol, amount, tail=10), 100)
        # 无有效样本 (源不带成交额, _normalize 填 0.0) → 按「手」
        self.assertEqual(volume_shares_mult(close, vol, [0.0] * 20), 100)
        self.assertEqual(volume_shares_mult(close, vol, None), 100)
        self.assertEqual(volume_shares_mult([], [], []), 100)

    def test_non_numeric_values_are_skipped_not_raised(self):
        """源里混进空串/占位符时按缺值跳过 —— object dtype 直接 astype(float) 会抛。"""
        close = ["10.0", "", "  ", 10.0]
        vol = ["100", 0, "", 200.0]
        amount = ["100000", 5, 7, 10.0 * 200 * 100]
        self.assertEqual(volume_shares_mult(close, vol, amount, tail=None), 100,
                         "空串既不该抛异常, 也不该被当成 0 参与判定")


class IntradayAvgPriceTest(unittest.TestCase):
    """分时均价 = 当日累计成交额 / 累计成交量 (只累加量额都有效的 bar)。"""

    def test_matches_vwap(self):
        close = [10.0, 10.2, 9.8]
        vol = [100.0, 200.0, 300.0]          # 手
        amount = [c * v * 100 for c, v in zip(close, vol)]
        got = intraday_avg_price(close, vol, amount)
        want = [10.0, (10 * 100 + 10.2 * 200) / 300, (10 * 100 + 10.2 * 200 + 9.8 * 300) / 600]
        for g, w in zip(got, want):
            self.assertAlmostEqual(g, w, places=9)

    def test_missing_amount_bar_is_excluded_from_denominator(self):
        """某根缺成交额: 它的成交量不能进分母 (否则之后每根均价都偏低, 偏差带到收盘)。"""
        close = [10.0, 10.2, 10.1, 10.3, 10.0]
        vol = [100.0, 200.0, 150.0, 300.0, 250.0]
        amount = [c * v * 100 for c, v in zip(close, vol)]
        amount[0] = 0.0            # 首根 (开盘集合竞价) 缺成交额
        got = intraday_avg_price(close, vol, amount)
        self.assertIsNone(got[0], "首根无有效成交额 → 该点 None")
        for i in range(1, len(close)):
            cv = sum(vol[1:i + 1])
            ca = sum(amount[1:i + 1])
            self.assertAlmostEqual(got[i], ca / (cv * 100), places=9,
                                   msg=f"第 {i} 根被缺额那根的成交量带偏了")
        # 对照: 若把缺额那根的量算进分母, 末根会明显偏低
        naive = sum(amount) / (sum(vol) * 100)
        self.assertGreater(got[-1], naive + 0.05, "偏差没被挡住")

    def test_middle_missing_amount_does_not_move_later_points(self):
        """中间某根缺成交额: 之后的均价只由有效 bar 决定, 不因缺额那根的量而下移。"""
        close = [10.0] * 5
        vol = [100.0] * 5
        amount = [100.0 * 100.0] * 5
        amount[2] = 0.0
        got = intraday_avg_price(close, vol, amount)
        self.assertAlmostEqual(got[1], 10.0, places=9)
        self.assertAlmostEqual(got[2], 10.0, places=9, msg="缺额那根沿用前值, 不引入量")
        self.assertAlmostEqual(got[4], 10.0, places=9)

    def test_all_missing_amount_is_none(self):
        close, vol = [10.0, 10.1], [100.0, 200.0]
        self.assertEqual(intraday_avg_price(close, vol, [0.0, 0.0]), [None, None])
        self.assertEqual(intraday_avg_price(close, vol, None), [None, None])
        self.assertEqual(intraday_avg_price([], [], []), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
