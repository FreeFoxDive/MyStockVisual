# -*- coding: utf-8 -*-
"""chips.compute_chips 东财筹码算法单测 (合成 bar, 无网络)。"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import chips


def _af_df(n=30, vol=1_000_000.0, close=10.0, amount_factor=100.0):
    """构造 AF 日K DataFrame; amount_factor=100 → volume 为手, =1 → 为股。"""
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": close, "high": close + 0.3, "low": close - 0.3,
        "close": close, "volume": vol,
        "amount": vol * amount_factor * close,
    }, index=idx)


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _bar(i, base=10.0, hsl=2.0):
    o = base + i * 0.1
    return {
        "date": f"2024-01-{i + 1:02d}",
        "open": o,
        "close": o + 0.2,
        "high": o + 0.3,
        "low": o - 0.1,
        "volume": 1_000_000,
        "amount": 10_000_000,
        "hsl": hsl,
    }


class TestComputeChips(unittest.TestCase):
    def test_weights_normalized_to_one(self):
        ch = chips.compute_chips([_bar(i) for i in range(40)])
        self.assertIsNotNone(ch)
        self.assertEqual(len(ch["buckets"]), chips.FACTOR)
        self.assertAlmostEqual(sum(b["weight"] for b in ch["buckets"]), 1.0, places=6)

    def test_summary_bounds(self):
        ch = chips.compute_chips([_bar(i) for i in range(40)])
        self.assertGreaterEqual(ch["profitRatio"], 0.0)
        self.assertLessEqual(ch["profitRatio"], 1.0)
        self.assertLessEqual(ch["pct90"][0], ch["pct90"][1])
        self.assertLessEqual(ch["pct70"][0], ch["pct70"][1])
        # 90% 成本区间必须比 70% 更宽
        self.assertLessEqual(ch["pct90"][0], ch["pct70"][0])
        self.assertGreaterEqual(ch["pct90"][1], ch["pct70"][1])
        self.assertGreaterEqual(ch["avgCost"], ch["min"])
        self.assertLessEqual(ch["avgCost"], ch["max"])

    def test_avg_cost_default_is_weighted_mean(self):
        ch = chips.compute_chips([_bar(i) for i in range(40)])
        buckets = ch["buckets"]
        total = sum(b["weight"] for b in buckets)
        wmean = sum(b["price"] * b["weight"] for b in buckets) / total
        # avgCost 为加权平均 (对齐东财 App), 与桶加权均值一致
        self.assertLess(abs(ch["avgCost"] - wmean), 0.02)

    def test_returns_weighted_and_median_costs(self):
        ch = chips.compute_chips([_bar(i) for i in range(40)])
        # 两种口径都返回, 均落在价格区间内且通常不相等 (分布右偏)
        self.assertIn("medianCost", ch)
        for key in ("avgCost", "medianCost"):
            self.assertGreaterEqual(ch[key], ch["min"])
            self.assertLessEqual(ch[key], ch["max"])
        self.assertNotAlmostEqual(ch["avgCost"], ch["medianCost"], places=2)

    def test_higher_turnover_pulls_average_toward_latest(self):
        low = chips.compute_chips([_bar(i, hsl=1.0) for i in range(40)])
        high = chips.compute_chips([_bar(i, hsl=30.0) for i in range(40)])
        # 换手越高, 筹码越集中在最新价格, 平均成本越靠近末根均价
        self.assertGreater(high["avgCost"], low["avgCost"])

    def test_flat_price_returns_none(self):
        bars = [{"date": "2024-01-01", "open": 10, "close": 10, "high": 10,
                 "low": 10, "volume": 1e6, "amount": 1e7, "hsl": 5.0}]
        self.assertIsNone(chips.compute_chips(bars))

    def test_empty_returns_none(self):
        self.assertIsNone(chips.compute_chips([]))

    def test_market_code(self):
        self.assertEqual(chips._market_code("600519.SH"), 1)
        self.assertEqual(chips._market_code("000001.SZ"), 0)
        self.assertEqual(chips._market_code("430047.BJ"), 0)


class TestFetchRowsFallback(unittest.TestCase):
    """URL 回退链 + 瞬时 503 重试 (不触网)。"""

    def _patches(self, fake_get, urls):
        return (
            mock.patch.object(chips, "_em_urls", return_value=urls),
            mock.patch.object(chips.requests, "get", side_effect=fake_get),
            mock.patch.object(chips.time, "sleep", lambda *_: None),
        )

    def test_https_fails_then_http_succeeds(self):
        calls = []

        def fake_get(url, **kw):
            calls.append(url)
            if url.startswith("http://push2his"):
                return _Resp(200, {"data": {"klines": ["row1", "row2"]}})
            raise RuntimeError("tls blocked")

        p1, p2, p3 = self._patches(fake_get, ["https://push2his/x", "http://push2his/y"])
        with p1, p2, p3:
            rows, err = chips._fetch_rows({})
        self.assertEqual(rows, ["row1", "row2"])
        self.assertIsNone(err)
        self.assertTrue(any(u.startswith("http://push2his") for u in calls))

    def test_retries_transient_503_then_succeeds(self):
        seq = [503, 503, 200]

        def fake_get(url, **kw):
            status = seq.pop(0) if seq else 200
            if status == 200:
                return _Resp(200, {"data": {"klines": ["ok"]}})
            return _Resp(status, None)

        p1, p2, p3 = self._patches(fake_get, ["http://push2his/z"])
        with p1, p2, p3:
            rows, err = chips._fetch_rows({})
        self.assertEqual(rows, ["ok"])
        self.assertIsNone(err)

    def test_all_fail_returns_error(self):
        def fake_get(url, **kw):
            raise RuntimeError("boom")

        p1, p2, p3 = self._patches(fake_get, ["https://a", "https://b"])
        with p1, p2, p3:
            rows, err = chips._fetch_rows({})
        self.assertIsNone(rows)
        self.assertIn("RuntimeError", err)


class TestFetchAfChipBars(unittest.TestCase):
    """AlphaFeed 兜底: 换手率 = 成交量(股)/流通股本 × 100 (不触网)。"""

    def test_lots_unit_hsl(self):
        import market as market_mod
        df = _af_df(n=30, vol=1_000_000.0, close=10.0, amount_factor=100.0)
        with mock.patch.object(market_mod, "_fetch_af_kline", return_value=df), \
             mock.patch.object(market_mod, "_fetch_instrument_meta",
                              return_value={"float_shares": 1.0e9}):
            bars = chips.fetch_af_chip_bars("600000.SH")
        self.assertEqual(len(bars), 30)
        # 1e6 手 ×100 = 1e8 股; /1e9 ×100 = 10%
        self.assertAlmostEqual(bars[-1]["hsl"], 10.0, places=4)

    def test_shares_unit_hsl(self):
        import market as market_mod
        df = _af_df(n=10, vol=1_000_000.0, close=10.0, amount_factor=1.0)
        with mock.patch.object(market_mod, "_fetch_af_kline", return_value=df), \
             mock.patch.object(market_mod, "_fetch_instrument_meta",
                              return_value={"float_shares": 1.0e9}):
            bars = chips.fetch_af_chip_bars("600000.SH")
        # 1e6 股 /1e9 ×100 = 0.1%
        self.assertAlmostEqual(bars[-1]["hsl"], 0.1, places=4)

    def test_no_float_shares_returns_none(self):
        import market as market_mod
        df = _af_df()
        with mock.patch.object(market_mod, "_fetch_af_kline", return_value=df), \
             mock.patch.object(market_mod, "_fetch_instrument_meta", return_value=None):
            self.assertIsNone(chips.fetch_af_chip_bars("600000.SH"))


class TestGetChipsSource(unittest.TestCase):
    BARS = [{"date": "2024-01-01", "open": 10.0, "close": 10.1,
             "high": 10.2, "low": 9.9, "volume": 1e6, "amount": 1e7,
             "hsl": 2.0}] * 30

    def setUp(self):
        chips._cache.clear()

    def test_default_is_af(self):
        with mock.patch.object(chips, "fetch_af_chip_bars", return_value=self.BARS) as af, \
             mock.patch.object(chips, "fetch_chip_bars", return_value=None) as em:
            res = chips.get_chips("600000.SH")
        self.assertEqual(res["source"], "af")
        af.assert_called_once()
        em.assert_not_called()

    def test_em_mode(self):
        with mock.patch.dict(os.environ, {"CHIPS_SOURCE": "em"}), \
             mock.patch.object(chips, "fetch_chip_bars", return_value=self.BARS) as em, \
             mock.patch.object(chips, "fetch_af_chip_bars", return_value=None) as af:
            res = chips.get_chips("600000.SH")
        self.assertEqual(res["source"], "em")
        em.assert_called_once()
        af.assert_not_called()

    def test_auto_falls_back_to_af(self):
        with mock.patch.dict(os.environ, {"CHIPS_SOURCE": "auto"}), \
             mock.patch.object(chips, "fetch_chip_bars", return_value=None) as em, \
             mock.patch.object(chips, "fetch_af_chip_bars", return_value=self.BARS) as af:
            res = chips.get_chips("600000.SH")
        self.assertEqual(res["source"], "af")
        em.assert_called_once()
        af.assert_called_once()


if __name__ == "__main__":
    unittest.main()
