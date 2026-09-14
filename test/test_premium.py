"""ETF 溢价异步 provider 单测: 向量化等价性 / 单飞 / 负缓存 / NAV 缓存。

溢价原先内联在 /api/kline 主路径 (akshare 净值无缓存无超时 + O(n²) 逐根掩码),
现改为后台 stale-while-revalidate。这里锁定两件事:
1. 向量化 ffill 对齐与原逐根循环**逐值等价** (口径不得漂移);
2. 不阻塞、不重复计算、失败有负缓存。
"""
from __future__ import annotations

import os
import sys
import threading
import unittest
from unittest import mock

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import market as market_mod
import premium as premium_mod


class _Fixture:
    """qfq 图表 df + 稀疏 NAV (周末无净值, 强制走 ffill) + 未复权 close。"""

    @staticmethod
    def build(periods=40):
        idx = pd.date_range("2026-01-01", periods=periods, freq="D")
        base = pd.Series(range(periods), index=idx, dtype=float)
        close = 4.0 + (base % 7) * 0.01
        df = pd.DataFrame(
            {"open": close, "high": close + 0.02, "low": close - 0.02,
             "close": close, "volume": 1_000_000.0, "amount": 10_000_000.0},
            index=idx,
        )
        nav_idx = idx[::3]
        nav = pd.DataFrame(
            {"nav": 3.9 + pd.Series(range(len(nav_idx)), index=nav_idx,
                                    dtype=float) * 0.005},
            index=nav_idx,
        )
        # 未复权日K: 与 market._fetch_af_kline 同形 (DataFrame 带 close 列)
        raw = pd.DataFrame({"close": close * 1.02}, index=idx)
        return df, nav, raw


def _legacy_premium(df, nav_df, raw_df):
    """原 /api/kline 内联实现 (O(n²) 布尔掩码), 仅作等价性对照。"""
    raw_close = None
    if raw_df is not None and len(raw_df) > 0:
        raw_close = raw_df["close"].copy()
        raw_close.index = pd.to_datetime(raw_close.index).normalize()
    df_sorted = df.sort_index()
    premiums = []
    for idx in df_sorted.index:
        nav_matches = nav_df[nav_df.index <= idx]
        if len(nav_matches) == 0:
            premiums.append(None)
            continue
        nav_val = float(nav_matches.iloc[-1]["nav"])
        close_val = None
        if raw_close is not None:
            idx_n = pd.Timestamp(idx).normalize()
            if idx_n in raw_close.index:
                close_val = float(raw_close.loc[idx_n])
            else:
                earlier = raw_close[raw_close.index <= idx_n]
                if len(earlier) > 0:
                    close_val = float(earlier.iloc[-1])
        if close_val is None:
            premiums.append(None)
            continue
        premiums.append((close_val - nav_val) / nav_val * 100 if nav_val > 0 else None)
    return premiums


class TestPremiumEquivalence(unittest.TestCase):
    SYM = "510300.SH"

    def setUp(self):
        premium_mod.clear()

    def _compute(self, df, nav, raw):
        with mock.patch.object(market_mod, "_is_etf", return_value=True), \
             mock.patch.object(market_mod, "_fetch_etf_nav", return_value=nav), \
             mock.patch.object(market_mod, "_fetch_af_kline", return_value=raw):
            return premium_mod.compute_premium(self.SYM, "1d", 1006, df=df)

    def _assert_same(self, got, want):
        self.assertEqual(len(got), len(want))
        for i, (g, w) in enumerate(zip(got, want)):
            if w is None:
                self.assertIsNone(g, f"第 {i} 根应为 None")
            else:
                self.assertIsNotNone(g, f"第 {i} 根不应为 None")
                self.assertAlmostEqual(g, w, places=9, msg=f"第 {i} 根溢价值漂移")

    def test_ffill_matches_legacy_loop(self):
        df, nav, raw = _Fixture.build()
        out = self._compute(df, nav, raw)
        self._assert_same(out["values"], _legacy_premium(df, nav, raw))

    def test_dates_align_with_chart_bars(self):
        df, nav, raw = _Fixture.build()
        out = self._compute(df, nav, raw)
        self.assertEqual(out["dates"], [str(i)[:10] for i in df.sort_index().index])

    def test_nav_newer_than_bars_uses_last_known(self):
        """NAV 比图更晚: 早于首个净值的 bar 无对齐值 → None (与旧实现一致)。"""
        df, _nav, raw = _Fixture.build(periods=12)
        late = pd.DataFrame({"nav": [4.0, 4.1]},
                            index=pd.to_datetime(["2026-01-20", "2026-01-25"]))
        out = self._compute(df, late, raw)
        self._assert_same(out["values"], _legacy_premium(df, late, raw))
        self.assertIsNone(out["values"][0])

    def test_no_raw_close_means_all_none(self):
        """无未复权对齐时不拿前复权价硬算 (避免拆分前溢价失真)。"""
        df, nav, _raw = _Fixture.build()
        with mock.patch.object(market_mod, "_is_etf", return_value=True), \
             mock.patch.object(market_mod, "_fetch_etf_nav", return_value=nav), \
             mock.patch.object(market_mod, "_fetch_af_kline", return_value=None):
            out = premium_mod.compute_premium(self.SYM, "1d", 1006, df=df)
        self.assertTrue(all(v is None for v in out["values"]))

    def test_non_etf_returns_none(self):
        df, _nav, _raw = _Fixture.build()
        with mock.patch.object(market_mod, "_is_etf", return_value=False):
            self.assertIsNone(
                premium_mod.compute_premium("000001.SZ", "1d", 1006, df=df))

    def test_no_nav_returns_none(self):
        df, _nav, _raw = _Fixture.build()
        with mock.patch.object(market_mod, "_is_etf", return_value=True), \
             mock.patch.object(market_mod, "_fetch_etf_nav", return_value=None):
            self.assertIsNone(
                premium_mod.compute_premium(self.SYM, "1d", 1006, df=df))


class TestPremiumSingleFlight(unittest.TestCase):
    SYM = "510300.SH"

    def setUp(self):
        premium_mod.clear()

    def test_request_is_single_flight_and_then_ready(self):
        """并发 request 只算一次; 算完 get 返回 ready=True。"""
        started = threading.Event()
        release = threading.Event()
        calls = []

        def _slow(symbol, period, count, df=None):
            calls.append(symbol)
            started.set()
            release.wait(5)
            return {"dates": ["2026-01-01"], "values": [1.0], "params": {}}

        with mock.patch.object(premium_mod, "compute_premium", side_effect=_slow):
            premium_mod.request(self.SYM, "1d", 1006)
            self.assertTrue(started.wait(5), "后台线程未启动")
            premium_mod.request(self.SYM, "1d", 1006)   # 单飞: 不得再起一个
            premium_mod.request(self.SYM, "1d", 1006)
            release.set()
            for _ in range(100):
                if premium_mod.get(self.SYM, "1d", 1006)["ready"]:
                    break
                threading.Event().wait(0.02)
        self.assertEqual(len(calls), 1, "单飞失败: 并发 request 重复计算")
        got = premium_mod.get(self.SYM, "1d", 1006)
        self.assertTrue(got["ready"])
        self.assertEqual(got["values"], [1.0])

    def test_get_before_ready_returns_flag_not_error(self):
        key = premium_mod._key(self.SYM, "1d", 1006)
        with mock.patch.object(premium_mod, "compute_premium", return_value=None):
            got = premium_mod.get(self.SYM, "1d", 1006)
        self.assertFalse(got["ready"])
        self.assertIn(key, premium_mod._fail_at, "失败应写入负缓存")

    def test_failure_negative_cache_blocks_retry(self):
        with mock.patch.object(premium_mod, "compute_premium") as cp:
            cp.side_effect = RuntimeError("akshare 挂了")
            premium_mod.request(self.SYM, "1d", 1006)
            for _ in range(100):
                if premium_mod._key(self.SYM, "1d", 1006) in premium_mod._fail_at:
                    break
                threading.Event().wait(0.02)
            premium_mod.request(self.SYM, "1d", 1006)  # 负缓存内不再重算
        self.assertEqual(cp.call_count, 1)

    def test_non_etf_request_does_nothing(self):
        with mock.patch.object(market_mod, "_is_etf", return_value=False), \
             mock.patch.object(premium_mod, "compute_premium") as cp:
            premium_mod.request("000001.SZ", "1d", 1006)
        cp.assert_not_called()

    def test_minute_period_not_supported(self):
        with mock.patch.object(market_mod, "_is_etf", return_value=True), \
             mock.patch.object(premium_mod, "compute_premium") as cp:
            premium_mod.request(self.SYM, "5m", 480)
        cp.assert_not_called()


class TestEtfNavCache(unittest.TestCase):
    """净值是日频数据: 同 symbol 重复取用只应打一次 akshare。"""

    def setUp(self):
        with market_mod._etf_nav_lock:
            market_mod._etf_nav_cache.clear()

    def test_nav_cached_between_calls(self):
        nav = pd.DataFrame({"nav": [4.0]}, index=pd.to_datetime(["2026-01-01"]))
        with mock.patch.object(market_mod, "_fetch_etf_nav_uncached",
                               return_value=nav) as fn:
            a = market_mod._fetch_etf_nav("510300.SH")
            b = market_mod._fetch_etf_nav("510300.SH")
        self.assertEqual(fn.call_count, 1, "净值未命中内存缓存")
        self.assertIs(a, b)

    def test_nav_failure_not_cached(self):
        with mock.patch.object(market_mod, "_fetch_etf_nav_uncached",
                               return_value=None) as fn:
            market_mod._fetch_etf_nav("510300.SH")
            market_mod._fetch_etf_nav("510300.SH")
        self.assertEqual(fn.call_count, 2, "失败不该写入净值缓存")


if __name__ == "__main__":
    unittest.main()
