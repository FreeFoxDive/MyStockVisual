# -*- coding: utf-8 -*-
"""因子库 (factors.py): 因子计算 / 存储往返 / 快照 / 调度门控 / 外部源降级 / 构建。

全部用例零网络: 行情与外部源一律 mock, 存储指向临时目录。

运行:
    venv/Scripts/python.exe -u visual/test/test_factors.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import factors  # noqa: E402
import screener_metrics as sm  # noqa: E402


def _df(closes, volumes=None, freq="D", start="2026-01-01", amount=None):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    volumes = np.asarray(volumes if volumes is not None else [1_000_000.0] * n, dtype=float)
    idx = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame({
        "open": closes * 0.995, "high": closes * 1.01, "low": closes * 0.99,
        "close": closes, "volume": volumes,
        "amount": np.asarray(amount if amount is not None else closes * volumes, dtype=float),
    }, index=idx)


def _rising(n=120, step=0.5, start=10.0):
    return _df([start + step * i for i in range(n)])


def _falling(n=120, step=0.5, start=70.0):
    return _df([start - step * i for i in range(n)])


def _dip_then_rally():
    """先跌后急涨: MA5 上穿 MA20 / MACD 金叉 / KDJ 金叉 都应出现。"""
    down = [30.0 - 0.25 * i for i in range(60)]
    up = [down[-1] + 0.9 * i for i in range(1, 41)]
    return _df(down + up)


class ComputeFactorsTest(unittest.TestCase):
    def test_too_short_returns_none(self):
        self.assertIsNone(factors.compute_factors(_rising(10), "600000.SH", "浦发"))

    def test_rising_series_flags(self):
        f = factors.compute_factors(_rising(), "600000.SH", "浦发银行")
        self.assertIsNotNone(f)
        for k in ("above_ma5", "above_ma10", "above_ma20", "above_ma60", "ma_bull_align"):
            self.assertTrue(f[k], k)
        self.assertTrue(f["new_high_20"] and f["new_high_60"])
        self.assertFalse(f["new_low_20"])
        self.assertGreater(f["chg_5d"], 0)
        self.assertGreaterEqual(f["up_streak"], 1)
        self.assertEqual(f["down_streak"], 0)
        self.assertGreater(f["close"], f["prev_close"])
        self.assertAlmostEqual(f["change_pct"], (f["close"] / f["prev_close"] - 1) * 100, places=6)

    def test_falling_series_flags(self):
        f = factors.compute_factors(_falling(), "600000.SH", "浦发银行")
        self.assertFalse(f["above_ma20"])
        self.assertFalse(f["ma_bull_align"])
        self.assertTrue(f["new_low_20"])
        self.assertLess(f["chg_20d"], 0)
        self.assertGreaterEqual(f["down_streak"], 1)
        self.assertEqual(f["up_streak"], 0)

    def test_cross_flags_match_indicator_library(self):
        """金叉/死叉口径必须与图表同一套指标库逐位一致 (防口径漂移)。"""
        from indicators import kdj, macd, sma
        df = _dip_then_rally()
        f = factors.compute_factors(df, "600000.SH", "浦发银行")
        c = df["close"]
        ma5, ma20 = sma(c, 5), sma(c, 20)
        self.assertEqual(f["ma5_cross_ma20"],
                         bool(ma5.iloc[-2] <= ma20.iloc[-2] and ma5.iloc[-1] > ma20.iloc[-1]))
        dif, dea, _h = macd(c, 12, 26, 9)
        self.assertEqual(f["macd_cross_up"],
                         bool(dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1]))
        k, d, _j = kdj(df["high"], df["low"], c, 9)
        self.assertEqual(f["kdj_golden"],
                         bool(k.iloc[-2] <= d.iloc[-2] and k.iloc[-1] > d.iloc[-1]))

    def test_cross_flags_true_when_cross_on_last_bar(self):
        # 40 根缓跌 + 20 根平 + 末根急涨: MA5 恰好在最后一根上穿 MA20
        closes = [30.0 - 0.05 * i for i in range(40)] + [28.0] * 20 + [46.0]
        f = factors.compute_factors(_df(closes), "600000.SH", "浦发银行")
        self.assertTrue(f["ma5_cross_ma20"], "末根急涨应触发 MA5 上穿 MA20")
        self.assertTrue(f["above_ma20"])
        self.assertTrue(f["above_ma5"])

    def test_volume_metrics(self):
        closes = [10.0 + 0.1 * i for i in range(120)]
        vols = [1_000_000.0] * 119 + [3_000_000.0]
        f = factors.compute_factors(_df(closes, vols), "600000.SH", "浦发")
        self.assertGreater(f["vol_ratio5"], 2.0)
        self.assertFalse(f["vol_shrink5"])
        vols2 = [1_000_000.0] * 119 + [400_000.0]
        f2 = factors.compute_factors(_df(closes, vols2), "600000.SH", "浦发")
        self.assertTrue(f2["vol_shrink5"])

    def test_turnover_needs_float_shares(self):
        df = _rising()
        self.assertIsNone(factors.compute_factors(df, "600000.SH", "浦发")["turnover"])
        f = factors.compute_factors(df, "600000.SH", "浦发", float_shares=1e9)
        self.assertIsNotNone(f["turnover"])
        self.assertAlmostEqual(f["turnover"], 1_000_000 / 1e9 * 100, places=6)

    def test_all_registry_fields_covered_locally(self):
        """注册表里非外部列必须都能算出来 (防止加指标时忘了算)。"""
        external = {"pe", "pb", "float_value", "total_value", "roe", "industry",
                    "main_net_inflow", "main_net_ratio", "lhb_today", "pledge_ratio",
                    "etf_premium"}
        df = _rising()
        feats = factors.compute_factors(df, "600000.SH", "浦发")
        feats.update(factors.compute_chip_factors(df, 1e9, 100.0))
        missing = {m["field"] for m in sm.METRICS} - external - set(feats.keys())
        self.assertFalse(missing, f"这些列没被产出: {sorted(missing)}")


class LimitFlagTest(unittest.TestCase):
    def test_main_board(self):
        self.assertEqual(factors._limit_flags("600000.SH", "浦发银行", 10.02, 10.0), (True, False))
        self.assertEqual(factors._limit_flags("600000.SH", "浦发银行", -10.02, 10.0), (False, True))
        self.assertEqual(factors._limit_flags("600000.SH", "浦发银行", 5.0, 10.0), (False, False))

    def test_growth_board_20pct(self):
        self.assertEqual(factors._limit_flags("300750.SZ", "宁德时代", 19.9, 300.0), (True, False))
        self.assertEqual(factors._limit_flags("688111.SH", "金山办公", 9.9, 300.0), (False, False))

    def test_st_5pct(self):
        self.assertEqual(factors._limit_flags("600001.SH", "ST某某", 5.0, 5.0), (True, False))

    def test_etf_and_non_numeric_skipped(self):
        self.assertEqual(factors._limit_flags("510300.SH", "沪深300ETF", 10.0, 4.0), (False, False))
        self.assertEqual(factors._limit_flags("00700.HK", "腾讯", 10.0, 400.0), (False, False))


class StreakTest(unittest.TestCase):
    def test_streaks(self):
        self.assertEqual(factors._streak(np.array([1.0, 2.0, 3.0, 4.0])), (3, 0))
        self.assertEqual(factors._streak(np.array([4.0, 3.0, 2.0, 1.0])), (0, 3))
        self.assertEqual(factors._streak(np.array([1.0, 2.0, 1.0])), (0, 1))
        self.assertEqual(factors._streak(np.array([5.0])), (0, 0))


class ChipFactorsTest(unittest.TestCase):
    def test_chip_columns_in_range(self):
        df = _rising(start=10.0)
        mult = 100.0
        out = factors.compute_chip_factors(df, float_shares=1e9, mult=mult)
        self.assertTrue(out)
        self.assertGreaterEqual(out["chip_profit"], 0.0)
        self.assertLessEqual(out["chip_profit"], 100.0)
        self.assertGreater(out["chip_concentration"], 0.0)
        self.assertIsInstance(out["above_chip_cost"], bool)

    def test_no_shares_no_chips(self):
        self.assertEqual(factors.compute_chip_factors(_rising(), None, 100.0), {})


class StoreTestCaseBase(unittest.TestCase):
    """临时目录 + 重置模块级缓存 (无测试方法, 供各用例类继承)。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="factors_test_")
        self._orig = (factors.CACHE_DIR, factors.DB_PATH, factors.SNAP_DIR, factors._ready)
        factors.CACHE_DIR = Path(self._tmp)
        factors.DB_PATH = Path(self._tmp) / "factors.db"
        factors.SNAP_DIR = Path(self._tmp) / "factors"
        factors._ready = False
        factors._snap_cache.update({"key": None, "df": None, "day": None})
        self.addCleanup(self._restore)

    def _restore(self):
        factors.CACHE_DIR, factors.DB_PATH, factors.SNAP_DIR, factors._ready = self._orig
        factors._snap_cache.update({"key": None, "df": None, "day": None})
        shutil.rmtree(self._tmp, ignore_errors=True)

class StoreTestCase(StoreTestCaseBase):
    """存储层: bars / meta / snapshot / builds。"""

    def test_bars_round_trip(self):
        factors.init_store()
        conn = factors._conn()
        try:
            self.assertTrue(factors.save_bars(conn, "600000.SH", "浦发银行", _rising(60)))
            conn.commit()
        finally:
            conn.close()
        df = factors.load_bars("600000.SH")
        self.assertEqual(len(df), 60)
        self.assertAlmostEqual(float(df["close"].iloc[-1]), 10.0 + 0.5 * 59, places=6)
        self.assertEqual(str(df.index[-1])[:10], "2026-03-01")
        self.assertIsNone(factors.load_bars("000001.SZ"))
        got = list(factors.iter_bars())
        self.assertEqual([g[0] for g in got], ["600000.SH"])
        self.assertEqual(got[0][1], "浦发银行")

    def test_shares_round_trip_and_age(self):
        factors.init_store()
        self.assertEqual(factors.load_shares(), {})
        self.assertIsNone(factors.shares_age_days())
        factors.save_shares({"600000.SH": (1.2e9, 1.5e9)})
        self.assertEqual(factors.load_shares()["600000.SH"], (1.2e9, 1.5e9))
        self.assertLess(factors.shares_age_days(), 0.01)

    def test_snapshot_write_read_and_version(self):
        factors.init_store()
        df = pd.DataFrame([{"symbol": "600000.SH", "close": 10.0},
                           {"symbol": "000001.SZ", "close": 11.0}])
        self.assertEqual(factors.available_days(), [])
        self.assertEqual(factors.snapshot(), (None, None))
        factors._write_snapshot("2026-09-18", df)
        self.assertEqual(factors.available_days(), ["2026-09-18"])
        self.assertEqual(factors.current_version(), "2026-09-18")
        got, day = factors.snapshot()
        self.assertEqual(day, "2026-09-18")
        self.assertEqual(len(got), 2)
        # 指定不存在的日期 → 回落最新
        _df2, day2 = factors.snapshot("2020-01-01")
        self.assertEqual(day2, "2026-09-18")

    def test_build_row_helpers(self):
        factors.init_store()
        self.assertIsNone(factors.get_build("2026-09-18"))
        factors._ensure_build_row("2026-09-18")
        self.assertEqual(factors.get_build("2026-09-18")["state"], "idle")
        self.assertEqual(factors._bump_attempts("2026-09-18"), 1)
        factors._update_build("2026-09-18", state="done", n_rows=5, percent=100,
                              sources='{"spot": "ok:1"}')
        row = factors.get_build("2026-09-18")
        self.assertEqual((row["state"], row["n_rows"], row["percent"]), ("done", 5, 100))
        self.assertEqual(len(factors.list_builds()), 1)


class ScheduleTest(StoreTestCaseBase):
    def _should(self, now, trading=True, at=(18, 0), snapshot_day=None, build_row=None):
        if snapshot_day:
            factors._write_snapshot(snapshot_day, pd.DataFrame([{"symbol": "X"}]))
        if build_row:
            factors._ensure_build_row(build_row["day"])
            factors._update_build(build_row["day"], **{k: v for k, v in build_row.items()
                                                       if k != "day"})
        with mock.patch.object(factors, "build_at", lambda: at), \
             mock.patch("market_hours.is_trading_day", lambda v=None: trading), \
             mock.patch.object(factors, "_now_dt", lambda: now):
            return factors.should_build(now)

    def test_non_trading_day_never_builds(self):
        # 周六 10:00, 快照已过期也不构建
        self.assertEqual(self._should(datetime(2026, 9, 19, 10, 0), trading=False,
                                      snapshot_day="2026-09-18"), (False, "non_trading_day"))

    def test_after_build_at_due_when_no_record(self):
        ok, why = self._should(datetime(2026, 9, 18, 18, 5), snapshot_day="2026-09-17")
        self.assertTrue(ok, why)
        self.assertEqual(why, "due")

    def test_already_done_skips(self):
        ok, why = self._should(datetime(2026, 9, 18, 19, 0), snapshot_day="2026-09-18",
                               build_row={"day": "2026-09-18", "state": "done", "attempts": 1})
        self.assertFalse(ok)
        self.assertEqual(why, "already_done")

    def test_before_build_at_never_builds(self):
        """回归: 未到 18:00 一律不构建 —— 包括"上一交易日漏跑"也不补建。"""
        # 09:00 交易日, 快照 = 上一交易日 → 不构建
        ok, why = self._should(datetime(2026, 9, 18, 9, 0), snapshot_day="2026-09-17")
        self.assertFalse(ok)
        self.assertEqual(why, "before_build_at")
        # 09:00 交易日, 最新快照还停在更早 → 仍然不构建 (顺延到下一个 18:00)
        ok2, why2 = self._should(datetime(2026, 9, 18, 9, 0), snapshot_day="2026-09-16")
        self.assertFalse(ok2, "晚间漏跑不在盘中补建")
        self.assertEqual(why2, "before_build_at")

    def test_catchup_removed_until_next_evening(self):
        """前夜漏跑的场景: 次日盘中不补, 次日 18:00 才构建当天的。"""
        # 次日 09:00: 最新快照是前前交易日 → 不构建
        self.assertEqual(self._should(datetime(2026, 9, 21, 9, 0),
                                      snapshot_day="2026-09-17"), (False, "before_build_at"))
        # 次日 18:05: 构建当天 (9/21), 不是补 9/18
        ok, why = self._should(datetime(2026, 9, 21, 18, 5), snapshot_day="2026-09-17")
        self.assertTrue(ok)
        self.assertEqual(why, "due")
        self.assertEqual(factors.due_day(datetime(2026, 9, 21, 18, 5)), "2026-09-21")

    def test_attempts_exhausted_stops_retry(self):
        ok, why = self._should(datetime(2026, 9, 18, 19, 0), snapshot_day="2026-09-17",
                               build_row={"day": "2026-09-18", "state": "error",
                                          "attempts": factors.MAX_ATTEMPTS})
        self.assertFalse(ok)
        self.assertEqual(why, "attempts_exhausted")

    def test_recent_failure_waits(self):
        ok, why = self._should(datetime(2026, 9, 18, 19, 0), snapshot_day="2026-09-17",
                               build_row={"day": "2026-09-18", "state": "running",
                                          "attempts": 1, "started_at": factors._iso()})
        self.assertFalse(ok)
        self.assertEqual(why, "recent_failure")

    def test_after_close_before_build_at_does_not_build_today(self):
        """回归: 交易日 15:00~18:00 不能把"今天"当构建目标抢跑。

        15:00 后 last_closed_trading_day() 已返回今天, 若拿它当"落后于它"的判据,
        补建条件恒真 → 龙虎榜(收盘后才有)/质押(15:30 才刷新) 必然缺列, 且 18:00 因
        already_done 不再构建。
        """
        for hm in ((15, 5), (16, 30), (17, 59)):
            ok, why = self._should(datetime(2026, 9, 18, *hm), snapshot_day="2026-09-17")
            self.assertFalse(ok, f"{hm[0]}:{hm[1]:02d} 不该构建")
            self.assertEqual(why, "before_build_at")

    def test_error_state_respects_retry_backoff(self):
        """回归: 退避要对 error 态生效 (此前只判 running, 失败会按调度间隔连撞)。"""
        now = datetime(2026, 9, 18, 19, 0)
        fresh = {"day": "2026-09-18", "state": "error", "attempts": 1,
                 "done_at": now.isoformat(timespec="seconds")}
        ok, why = self._should(now, snapshot_day="2026-09-17", build_row=fresh)
        self.assertFalse(ok)
        self.assertEqual(why, "recent_failure")
        # 超过 RETRY_MIN_SEC 之后才允许重试
        older = now - timedelta(minutes=factors.RETRY_MIN_SEC // 60 + 1)
        ok2, why2 = self._should(now, snapshot_day="2026-09-17",
                                 build_row=dict(fresh,
                                                done_at=older.isoformat(timespec="seconds")))
        self.assertTrue(ok2, why2)
        self.assertEqual(why2, "due")


class DueDayTest(StoreTestCaseBase):
    """due_day: 只有交易日 18:00 后才返回今天, 其余一律 None (不构建)。"""

    def test_only_after_build_at(self):
        with mock.patch("market_hours.is_trading_day", lambda v=None: True):
            self.assertIsNone(factors.due_day(datetime(2026, 9, 18, 10, 0)))
            self.assertIsNone(factors.due_day(datetime(2026, 9, 18, 15, 5)))
            self.assertIsNone(factors.due_day(datetime(2026, 9, 18, 17, 59)))
            self.assertEqual(factors.due_day(datetime(2026, 9, 18, 18, 0)), "2026-09-18")
            self.assertEqual(factors.due_day(datetime(2026, 9, 18, 23, 30)), "2026-09-18")

    def test_non_trading_day_returns_none(self):
        with mock.patch("market_hours.is_trading_day", lambda v=None: False):
            self.assertIsNone(factors.due_day(datetime(2026, 9, 19, 19, 0)))

    def test_before_build_at_helper(self):
        with mock.patch.object(factors, "build_at", lambda: (18, 0)):
            self.assertTrue(factors.before_build_at(datetime(2026, 9, 18, 17, 59)))
            self.assertFalse(factors.before_build_at(datetime(2026, 9, 18, 18, 0)))


class ManualBuildWindowTest(StoreTestCaseBase):
    """手动入口同样受"只在 18:00 后"约束, force / 显式 day 例外。"""

    def test_manual_build_before_window_is_refused(self):
        factors.init_store()
        with mock.patch.object(factors, "before_build_at", lambda now=None: True), \
             mock.patch.object(factors, "fetch_bars") as fb:
            out = factors.build(day=None)
            # 手动入口 (管理页「手动重建」/ CLI 不带 force) 同样被拒, 且如实给原因
            # —— 断言必须在 patch 作用域内, 否则用的是真实时钟
            self.assertIn("18:00", factors.build_blocked_reason())
            self.assertIn("18:00", factors.build(day=None)["message"])
        self.assertFalse(out["ok"])
        self.assertEqual(out["reason"], "before_build_at")
        fb.assert_not_called()

    def test_force_or_explicit_day_bypasses_window(self):
        factors.init_store()
        with mock.patch.object(factors, "before_build_at", lambda now=None: True), \
             mock.patch.object(factors, "universe", lambda: [("600000.SH", "浦发")]), \
             mock.patch.object(factors, "fetch_bars", lambda syms, **kw: {}), \
             mock.patch.object(factors, "external_meta",
                               lambda day, syms: ({}, {"spot": "ok:0"})), \
             mock.patch.object(factors, "_notify_state", lambda *a, **k: None):
            self.assertIsNone(factors.build_blocked_reason(force=True))
            out = factors.build(day="2026-09-17")          # 显式指定日期 = 回补, 放行
            self.assertIsNone(out.get("reason"))
            self.assertTrue(out["ok"])


class LiveSnapshotTest(StoreTestCaseBase):
    """盘中口径只在连续竞价/午休拼接; 竞价与盘后一律不拼 (会凭空多一根假 bar)。"""

    def setUp(self):
        super().setUp()
        factors.init_store()
        factors._write_snapshot("2026-09-18", pd.DataFrame([{"symbol": "600000.SH",
                                                             "close": 10.0}]))

    def test_auction_and_closed_are_skipped(self):
        for phase in ("auction", "pre", "closed", "non_trading"):
            with mock.patch("market_hours.session_phase", lambda v=None, p=phase: p), \
                 mock.patch.object(factors, "_fetch_quotes") as fetch:
                self.assertEqual(factors.live_snapshot(), (None, None), phase)
                fetch.assert_not_called()

    def test_trading_phase_proceeds_to_fetch(self):
        with mock.patch("market_hours.session_phase", lambda v=None: "trading"), \
             mock.patch.object(factors, "_fetch_quotes", return_value={}) as fetch:
            self.assertEqual(factors.live_snapshot(), (None, None), "无快照数据 → 回退")
        self.assertTrue(fetch.called, "连续竞价应当去取快照")


class StatusTest(StoreTestCaseBase):
    def test_status_shape_and_five_days(self):
        factors.init_store()
        factors._write_snapshot("2026-09-18", pd.DataFrame([{"symbol": "X", "close": 1.0}]))
        factors._ensure_build_row("2026-09-18")
        factors._update_build("2026-09-18", state="done", n_rows=1, n_symbols=2,
                              n_missing=1, percent=100)
        st = factors.status()
        self.assertEqual(st["snapshot_day"], "2026-09-18")
        self.assertEqual(len(st["last_days"]), 5)
        self.assertEqual(st["last_days"][0]["day"], "2026-09-18")
        self.assertEqual(st["last_days"][0]["state"], "done")
        self.assertIn("build_at", st)
        for row in st["last_days"]:
            self.assertIn("state", row)


class ExternalMetaTest(unittest.TestCase):
    def test_all_sources_failing_is_recorded_not_raised(self):
        broken = mock.Mock(side_effect=RuntimeError("源挂了"))
        with mock.patch.object(factors, "_src_spot", broken), \
             mock.patch.object(factors, "_src_yjbb", broken), \
             mock.patch.object(factors, "_src_flow", broken), \
             mock.patch.object(factors, "_src_lhb", broken), \
             mock.patch.object(factors, "_src_pledge", broken), \
             mock.patch.object(factors, "_src_etf_premium", broken):
            rows, sources = factors.external_meta("2026-09-18", ["600000.SH", "000001.SZ"])
        self.assertEqual(rows, {})
        self.assertEqual(len(sources), 6)
        for name, status in sources.items():
            self.assertTrue(status.startswith("fail:"), f"{name}={status}")

    def test_sources_merge_into_symbol_rows(self):
        with mock.patch.object(factors, "_src_spot", lambda: {"600000": {"pe": 6.5, "pb": 0.6}}), \
             mock.patch.object(factors, "_src_yjbb",
                               lambda day: {"_period": "20260630",
                                            "rows": {"600000": {"roe": 11.2, "industry": "银行"}}}), \
             mock.patch.object(factors, "_src_flow",
                               lambda: {"600000": {"main_net_inflow": 1.5e7, "main_net_ratio": 3.2}}), \
             mock.patch.object(factors, "_src_lhb", lambda day: {"600000": True}), \
             mock.patch.object(factors, "_src_pledge", lambda: {"600000": 4.4}), \
             mock.patch.object(factors, "_src_etf_premium", lambda: {"510300": -0.3}):
            rows, sources = factors.external_meta(
                "2026-09-18", ["600000.SH", "000001.SZ", "510300.SH"])
        self.assertEqual(rows["600000.SH"]["pe"], 6.5)
        self.assertEqual(rows["600000.SH"]["roe"], 11.2)
        self.assertEqual(rows["600000.SH"]["industry"], "银行")
        self.assertTrue(rows["600000.SH"]["lhb_today"])
        self.assertEqual(rows["600000.SH"]["pledge_ratio"], 4.4)
        self.assertEqual(rows["510300.SH"]["etf_premium"], -0.3)
        self.assertNotIn("000001.SZ", rows, "无数据的标的不进结果")
        self.assertIn("ok:", sources["yjbb"])


class BuildTest(StoreTestCaseBase):
    """build(): mock 取数与外部源, 断言 bars/快照/builds 落库与通知。"""

    def _frames(self):
        return {"600000.SH": _rising(120), "000001.SZ": _falling(120)}

    def test_build_writes_snapshot_and_records(self):
        factors.init_store()
        notes = []
        with mock.patch.object(factors, "universe",
                               lambda: [("600000.SH", "浦发银行"), ("000001.SZ", "平安银行")]), \
             mock.patch.object(factors, "fetch_bars", lambda syms, **kw: self._frames()), \
             mock.patch.object(factors, "_fetch_shares",
                               lambda syms: {"600000.SH": (1e9, 1.2e9)}), \
             mock.patch.object(factors, "external_meta",
                               lambda day, syms: ({"600000.SH": {"pe": 6.0, "industry": "银行"}},
                                                  {"spot": "ok:1", "yjbb": "fail:KeyError"})), \
             mock.patch.object(factors, "_notify_state",
                               lambda kind, day, extra="": notes.append((kind, day))):
            out = factors.build(day="2026-09-18", notify=True)
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["n_rows"], 2)
        self.assertEqual([n[0] for n in notes], ["start", "done"])
        row = factors.get_build("2026-09-18")
        self.assertEqual(row["state"], "done")
        self.assertEqual(row["percent"], 100)
        self.assertIn("yjbb", row["sources"])
        df, day = factors.snapshot()
        self.assertEqual(day, "2026-09-18")
        self.assertEqual(set(df["symbol"]), {"600000.SH", "000001.SZ"})
        self.assertEqual(df.set_index("symbol").loc["600000.SH", "pe"], 6.0)
        self.assertEqual(df.set_index("symbol").loc["600000.SH", "industry"], "银行")
        # 市值兜底: 有股本的标的用 close × float_shares 估
        v = df.set_index("symbol").loc["600000.SH", "float_value"]
        self.assertIsNotNone(v)
        self.assertGreater(v, 0)
        self.assertEqual(len(factors.load_bars("600000.SH")), 120)

    def test_build_skips_when_already_done(self):
        factors.init_store()
        factors._ensure_build_row("2026-09-18")
        factors._update_build("2026-09-18", state="done", n_rows=7)
        with mock.patch.object(factors, "fetch_bars") as fb:
            out = factors.build(day="2026-09-18")
        self.assertEqual(out.get("reason"), "already")
        fb.assert_not_called()

    def test_build_error_marks_row_and_notifies(self):
        factors.init_store()
        notes = []
        with mock.patch.object(factors, "universe", lambda: [("600000.SH", "浦发")]), \
             mock.patch.object(factors, "fetch_bars",
                               side_effect=RuntimeError("上游挂了")), \
             mock.patch.object(factors, "_notify_state",
                               lambda kind, day, extra="": notes.append(kind)):
            out = factors.build(day="2026-09-18")
        self.assertFalse(out["ok"])
        row = factors.get_build("2026-09-18")
        self.assertEqual(row["state"], "error")
        self.assertIn("挂", row["error"])
        self.assertEqual(notes, ["start", "error"])

    def test_error_text_is_sanitized(self):
        """异常原文不进库/不进推送 (只留脱敏归类文案)。"""
        factors.init_store()
        secret = "api_key=SECRET123 https://internal.example.com/x"
        with mock.patch.object(factors, "universe", lambda: [("600000.SH", "浦发")]), \
             mock.patch.object(factors, "fetch_bars", side_effect=RuntimeError(secret)), \
             mock.patch.object(factors, "_notify_state", lambda *a, **k: None):
            out = factors.build(day="2026-09-18")
        self.assertNotIn("SECRET123", out["error"])
        self.assertNotIn("internal.example.com", factors.get_build("2026-09-18")["error"])

    def test_running_flag_reset_when_prologue_raises(self):
        """回归: attempts/状态更新抛错时 running 必须复位, 否则永久卡 running。"""
        factors.init_store()
        with mock.patch.object(factors, "universe", lambda: [("600000.SH", "浦发")]), \
             mock.patch.object(factors, "fetch_bars", lambda syms, **kw: {}), \
             mock.patch.object(factors, "_bump_attempts",
                               side_effect=RuntimeError("database is locked")), \
             mock.patch.object(factors, "_notify_state", lambda *a, **k: None):
            out = factors.build(day="2026-09-18")
        self.assertFalse(out["ok"])
        self.assertFalse(factors._build_state["running"])
        # 还能继续用 (不是"永久 running")
        self.assertIsNone(factors.build_blocked_reason(day="2026-09-18", force=True))

    def test_blocked_reason_reports_truthfully(self):
        factors.init_store()
        factors._ensure_build_row("2026-09-18")
        factors._update_build("2026-09-18", state="done", n_rows=3)
        reason = factors.build_blocked_reason(day="2026-09-18")
        self.assertIn("已构建完成", reason)
        self.assertIsNone(factors.build_blocked_reason(day="2026-09-18", force=True))
        factors._update_build("2026-09-18", state="error", attempts=factors.MAX_ATTEMPTS)
        self.assertIn("已失败", factors.build_blocked_reason(day="2026-09-18") or "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
