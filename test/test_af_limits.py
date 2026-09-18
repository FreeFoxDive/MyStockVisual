# -*- coding: utf-8 -*-
"""AlphaFeed Pro 限额表与取数预算折算 (af_limits)。

回归点: 限额表被写错/取整方式被改 (90% 折算) 会让选股全市场拉K撞上上游 429。

运行:
    venv/Scripts/python.exe -u visual/test/test_af_limits.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import af_limits  # noqa: E402


class AfLimitsTest(unittest.TestCase):
    def test_table_matches_pro_plan(self):
        """Pro 套餐限额 (次/分钟) —— 改动必须来自套餐文档, 不能凭感觉调。"""
        self.assertEqual(af_limits.AF_PRO_LIMITS, {
            "quotes_symbol": 120,
            "quotes_universe": 60,
            "kline_daily_batch": 60,
            "kline_daily_symbol": 120,
            "kline_minute_batch": 30,
            "kline_minute_symbol": 60,
            "intraday_symbol": 60,
            "depth_batch": 30,
            "depth_symbol": 60,
            "adj_factor_symbol": 60,
        })

    def test_batch_size_is_100(self):
        self.assertEqual(af_limits.BATCH_SIZE, 100)

    def test_bucket_rate_defaults_to_ninety_percent(self):
        self.assertEqual(af_limits.RESERVE_RATIO, 0.9)
        self.assertEqual(af_limits.bucket_rate("kline_daily_batch"), 54)    # 60 × 90%
        self.assertEqual(af_limits.bucket_rate("kline_minute_batch"), 27)   # 30 × 90%
        self.assertEqual(af_limits.bucket_rate("kline_daily_symbol"), 108)  # 120 × 90%
        self.assertEqual(af_limits.bucket_rate("depth_batch"), 27)

    def test_explicit_ratio_and_floor(self):
        self.assertEqual(af_limits.bucket_rate("kline_daily_batch", 1.0), 60)
        self.assertEqual(af_limits.bucket_rate("kline_daily_batch", 0.5), 30)
        # 折算后不允许为 0 (令牌桶速率至少 1)
        self.assertEqual(af_limits.bucket_rate("quotes_universe", 0.001), 1)
        for bad in (0, -0.5):
            with self.assertRaises(ValueError):
                af_limits.bucket_rate("kline_daily_batch", bad)

    def test_interval_matches_rate(self):
        self.assertAlmostEqual(af_limits.bucket_interval("kline_daily_batch"), 60 / 54)

    def test_unknown_name_raises(self):
        with self.assertRaises(KeyError):
            af_limits.limit("kline_weekly_batch")
        with self.assertRaises(KeyError):
            af_limits.bucket_rate("bogus")

    def test_describe_covers_every_limit(self):
        text = af_limits.describe()
        for name in af_limits.AF_PRO_LIMITS:
            self.assertIn(name, text)
        self.assertIn("100 标的", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
