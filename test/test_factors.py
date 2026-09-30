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


def _df(closes, volumes=None, freq="D", start="2026-01-01", amount=None, end=None):
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    volumes = np.asarray(volumes if volumes is not None else [1_000_000.0] * n, dtype=float)
    if end:
        idx = pd.date_range(end=end, periods=n, freq=freq)
    else:
        idx = pd.date_range(start, periods=n, freq=freq)
    return pd.DataFrame({
        "open": closes * 0.995, "high": closes * 1.01, "low": closes * 0.99,
        "close": closes, "volume": volumes,
        "amount": np.asarray(amount if amount is not None else closes * volumes, dtype=float),
    }, index=idx)


def _gen_bars(frames, batches=None, failed=0, size=None):
    """fetch_bars 的替身: 与真实实现一样按批 yield (batch, stats)。

    size=None 时整份一批; 给 size 则每 size 只一批, 用来验证流式构建。
    """
    def fake(symbols, **kw):
        items = list(frames.items())
        step = size or max(1, len(items))
        chunks = [dict(items[i:i + step]) for i in range(0, len(items), step)] or [{}]
        total = batches if batches is not None else len(chunks)
        for i, chunk in enumerate(chunks, 1):
            yield chunk, {"batches": total, "done": i, "failed_batches": failed}
    return fake


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
        # 创业板/科创板 ST 是 20% 不是 5%
        self.assertEqual(factors._limit_flags("300001.SZ", "ST宁德", 5.0, 10.0), (False, False))
        self.assertEqual(factors._limit_flags("300001.SZ", "*ST宁德", 19.9, 10.0), (True, False))
        self.assertEqual(factors._limit_flags("688001.SH", "ST金山", 19.9, 10.0), (True, False))

    def test_bse_30pct(self):
        self.assertEqual(factors._limit_flags("830001.BJ", "北交示例", 29.9, 10.0), (True, False))
        self.assertEqual(factors._limit_flags("920001.BJ", "北交新码", 29.9, 10.0), (True, False))
        self.assertEqual(factors._limit_flags("830001.BJ", "ST北交", 9.9, 10.0), (False, False))
        self.assertEqual(factors._limit_flags("830001.BJ", "ST北交", 29.9, 10.0), (True, False))
        self.assertEqual(factors._limit_flags("430001.BJ", "老三板", 10.0, 10.0), (False, False))

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
             mock.patch.object(factors, "fetch_bars", _gen_bars({})), \
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
        factors._live_cache.update({"day": None, "ts": 0.0, "df": None, "version": None})
        with mock.patch("market_hours.session_phase", lambda v=None: "trading"), \
             mock.patch.object(factors, "_fetch_quotes", return_value={}) as fetch:
            df, ver = factors.live_snapshot()
        self.assertIsNone(ver, "无快照数据 → 回退")
        self.assertTrue(fetch.called, "连续竞价应当去取快照")


class SchedulerSpawnTest(unittest.TestCase):
    """调度线程拉起子进程而不是在 Web 进程里算 (全市场计算约 2 分钟, 会占住 GIL)。"""

    def test_spawn_build_uses_subprocess(self):
        import subprocess
        with mock.patch("subprocess.Popen") as popen:
            popen.return_value = mock.Mock()
            factors._spawn_build("2026-09-18")
        cmd = popen.call_args.args[0]
        self.assertIn("--build", cmd)
        self.assertIn("--day", cmd)
        self.assertIn("2026-09-18", cmd)
        self.assertTrue(cmd[0].endswith("python.exe") or cmd[0].endswith("python")
                        or "python" in cmd[0])

    def test_loop_does_not_respawn_while_child_runs(self):
        child = mock.Mock()
        child.poll.return_value = None          # 还在跑
        spawned = []

        def fake_spawn(day):
            spawned.append(day)
            return child

        sleeps = {"n": 0}

        def fake_sleep(_sec):
            sleeps["n"] += 1
            if sleeps["n"] >= 2:
                raise SystemExit

        with mock.patch.object(factors, "init_store", lambda: None), \
             mock.patch.object(factors, "should_build", lambda now=None: (True, "due")), \
             mock.patch.object(factors, "due_day", lambda now=None: "2026-09-18"), \
             mock.patch.object(factors, "_spawn_build", fake_spawn), \
             mock.patch("time.sleep", fake_sleep):
            with self.assertRaises(SystemExit):
                factors._loop()
        self.assertEqual(spawned, ["2026-09-18"], "子进程还在跑时不能再拉起一个")


class StatusTest(StoreTestCaseBase):
    def test_status_shape_and_five_days(self):
        factors.init_store()
        factors._write_snapshot("2026-09-18", pd.DataFrame([{"symbol": "X", "close": 1.0}]))
        factors._ensure_build_row("2026-09-18")
        factors._update_build("2026-09-18", state="done", n_rows=1, n_symbols=2,
                              n_missing=1, percent=100)
        now = datetime(2026, 9, 18, 20, 0)
        # 时钟被拨到 18:00 之后时, 同进程里的调度线程不能跟着去拉真实行情
        with mock.patch.object(factors, "_now_dt", lambda: now), \
             mock.patch("market_hours.is_trading_day", lambda v=None: True), \
             mock.patch.object(factors, "build", lambda *a, **k: {"ok": False}):
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

    def _frames(self, day="2026-09-18"):
        # 最后一根必须落在构建日, 否则按停牌剔除
        return {"600000.SH": _df([10.0 + 0.5 * i for i in range(120)], end=day),
                "000001.SZ": _df([70.0 - 0.3 * i for i in range(120)], end=day)}

    def test_build_writes_snapshot_and_records(self):
        factors.init_store()
        notes = []
        with mock.patch.object(factors, "universe",
                               lambda: [("600000.SH", "浦发银行"), ("000001.SZ", "平安银行")]), \
             mock.patch.object(factors, "fetch_bars",
                               _gen_bars(self._frames(), batches=4, failed=1)), \
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
        self.assertIn('"bars": "fail:1/4批"', row["sources"], "失败批次要记进 sources")
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
             mock.patch.object(factors, "fetch_bars", _gen_bars({})), \
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

    def test_bars_truncated_to_day_and_stale_dropped(self):
        factors.init_store()
        # 序列延伸到 09-20; 按 09-18 构建时收盘价必须是 09-18 那根, 不能是更新的
        extended = _df(list(range(1, 41)), end="2026-09-20")
        stale = _df([10.0 + i for i in range(40)], end="2026-09-17")
        frames = {"600000.SH": extended, "000001.SZ": stale}
        with mock.patch.object(factors, "universe",
                               lambda: [("600000.SH", "浦发"), ("000001.SZ", "平安")]), \
             mock.patch.object(factors, "fetch_bars", _gen_bars(frames, batches=1)), \
             mock.patch.object(factors, "external_meta", lambda day, syms: ({}, {})), \
             mock.patch.object(factors, "_notify_state", lambda *a, **k: None):
            out = factors.build(day="2026-09-18")
        self.assertTrue(out["ok"], out)
        df, day = factors.snapshot()
        self.assertEqual(day, "2026-09-18")
        self.assertEqual(list(df["symbol"]), ["600000.SH"])
        # end=09-20 的最后三根是 09-18/19/20, 对应 close 38/39/40
        self.assertAlmostEqual(float(df.iloc[0]["close"]), 38.0)
        self.assertEqual(out["n_missing"], 1)

    def test_bad_day_rejected(self):
        factors.init_store()
        for bad in ("nope", "2026/09/18", "2026-09-19", "2099-01-04"):
            out = factors.build(day=bad, force=True)
            self.assertFalse(out["ok"], bad)
            self.assertEqual(out["reason"], "bad_day", bad)
            self.assertIsNotNone(factors.build_blocked_reason(day=bad, force=True), bad)

    def test_start_notice_counts_attempts(self):
        factors.init_store()
        factors._ensure_build_row("2026-09-18")
        factors._update_build("2026-09-18", state="error", attempts=1)
        notes = []
        with mock.patch.object(factors, "universe", lambda: []), \
             mock.patch.object(factors, "fetch_bars", _gen_bars({})), \
             mock.patch.object(factors, "external_meta", lambda day, syms: ({}, {})), \
             mock.patch.object(factors, "_notify_state",
                               lambda kind, day, extra="": notes.append((kind, extra))):
            out = factors.build(day="2026-09-18")
        self.assertTrue(out["ok"], out)
        self.assertEqual(notes[0][0], "start")
        self.assertIn("第 2 次", notes[0][1])

    def test_afternoon_force_is_rebuilt_after_build_at(self):
        factors.init_store()
        afternoon = datetime(2026, 9, 18, 16, 0)
        with mock.patch.object(factors, "_now_dt", lambda: afternoon), \
             mock.patch.object(factors, "before_build_at", lambda now=None: True), \
             mock.patch.object(factors, "universe", lambda: []), \
             mock.patch.object(factors, "fetch_bars", _gen_bars({})), \
             mock.patch.object(factors, "external_meta", lambda day, syms: ({}, {"spot": "ok:0"})), \
             mock.patch.object(factors, "_notify_state", lambda *a, **k: None), \
             mock.patch("market_hours.is_trading_day", lambda v=None: True):
            out = factors.build(day="2026-09-18", force=True)
        self.assertTrue(out["ok"], out)
        row = factors.get_build("2026-09-18")
        self.assertIn("_provisional", row["sources"])
        evening = datetime(2026, 9, 18, 19, 0)
        with mock.patch("market_hours.is_trading_day", lambda v=None: True), \
             mock.patch.object(factors, "build_at", lambda: (18, 0)), \
             mock.patch.object(factors, "_now_dt", lambda: evening):
            ok, why = factors.should_build(evening)
            blocked = factors.build_blocked_reason(day="2026-09-18")
        self.assertTrue(ok, why)
        self.assertEqual(why, "due")
        self.assertIsNone(blocked, "临时快照到点后手动重建也不能报「已构建完成」")


class AsOfAndLiveBarTest(unittest.TestCase):
    def test_bars_as_of_requires_that_day(self):
        df = _df([float(i) for i in range(40)], end="2026-09-17")
        self.assertIsNone(factors._bars_as_of(df, "2026-09-18"))
        kept = factors._bars_as_of(_df([float(i) for i in range(40)], end="2026-09-18"),
                                   "2026-09-18")
        self.assertEqual(str(kept.index[-1])[:10], "2026-09-18")

    def test_quote_without_volume_or_stale_ts_is_not_today(self):
        self.assertFalse(factors._quote_is_today({"volume": 0, "last_price": 10}, "2026-09-18"))
        self.assertFalse(factors._quote_is_today({"volume": None}, "2026-09-18"))
        # 2026-09-17 15:00 CST
        import datetime as dt_mod
        ts = dt_mod.datetime(2026, 9, 17, 15, 0,
                             tzinfo=dt_mod.timezone(dt_mod.timedelta(hours=8))).timestamp()
        self.assertFalse(factors._quote_is_today(
            {"volume": 100, "timestamp": ts}, "2026-09-18"))
        self.assertTrue(factors._quote_is_today({"volume": 100}, "2026-09-18"))

    def test_patch_updates_amount_and_skips_zero_volume(self):
        bars = _df([10.0] * 40, end="2026-09-17")
        untouched = factors._patch_last_bar(
            bars, "2026-09-18", {"volume": 0, "last_price": 11}, 11)
        self.assertEqual(len(untouched), len(bars))
        patched = factors._patch_last_bar(
            bars, "2026-09-18",
            {"volume": 123, "amount": 456, "high": 12, "low": 9, "open": 10}, 11)
        self.assertEqual(str(patched.index[-1])[:10], "2026-09-18")
        self.assertAlmostEqual(float(patched["close"].iloc[-1]), 11.0)
        self.assertAlmostEqual(float(patched["amount"].iloc[-1]), 456.0)
        self.assertAlmostEqual(float(patched["volume"].iloc[-1]), 123.0)
        # 同日替换也要改 amount, 不能只改 close
        same_day = _df([10.0] * 40, end="2026-09-18")
        replaced = factors._patch_last_bar(
            same_day, "2026-09-18",
            {"volume": 200, "amount": 999, "high": 12, "low": 9, "open": 10}, 11)
        self.assertEqual(len(replaced), len(same_day))
        self.assertAlmostEqual(float(replaced["amount"].iloc[-1]), 999.0)
        self.assertAlmostEqual(float(replaced["close"].iloc[-1]), 11.0)

    def test_fetch_quotes_imports_rate(self):
        af = mock.Mock()
        frame = pd.DataFrame([{"symbol": "600000.SH", "last_price": 10.0, "volume": 100}])
        af.quotes.get.return_value = frame
        seen = []
        with mock.patch("market.get_af", return_value=af):
            out = factors._fetch_quotes(["600000.SH"],
                                        progress_cb=lambda d, t: seen.append((d, t)))
        self.assertEqual(out["600000.SH"]["last_price"], 10.0)
        self.assertEqual(seen, [(1, 1)], "取快照期间要回报进度")
        self.assertLessEqual(factors.live_quote_rate(), 108)

    def test_live_snapshot_reads_bars_once_and_skips_stale(self):
        """有当日快照的标的一次读出; 无快照/量为 0 的不读K、不重算。"""
        base = pd.DataFrame([
            {"symbol": "600000.SH", "name": "浦发", "close": 10.0, "prev_close": 9.5},
            {"symbol": "000001.SZ", "name": "平安", "close": 12.0, "prev_close": 12.0},
        ])
        quotes = {"600000.SH": {"last_price": 11.0, "volume": 100, "amount": 1100,
                                "high": 11.2, "low": 10.5, "open": 10.6},
                  "000001.SZ": {"last_price": 12.5, "volume": 0}}
        calls = {"load": 0, "compute": 0}

        def load_many(symbols):
            calls["load"] += 1
            self.assertEqual(symbols, ["600000.SH"], "量为 0 的标的不该读K")
            return {"600000.SH": _df([10.0] * 40, end="2026-09-17")}

        def compute(df, symbol="", name="", float_shares=None):
            calls["compute"] += 1
            return {"symbol": symbol, "close": float(df["close"].iloc[-1])}

        factors._live_cache.update({"day": None, "ts": 0.0, "df": None, "version": None})
        with mock.patch.object(factors, "snapshot", lambda day=None: (base, "2026-09-18")), \
             mock.patch.object(factors, "_now_dt",
                               lambda: datetime(2026, 9, 18, 10, 30)), \
             mock.patch("market_hours.session_phase", lambda now=None: "trading"), \
             mock.patch.object(factors, "_fetch_quotes", lambda syms, progress_cb=None: quotes), \
             mock.patch.object(factors, "load_bars_many", load_many), \
             mock.patch.object(factors, "load_shares", lambda: {}), \
             mock.patch.object(factors, "compute_factors", compute):
            df, ver = factors.live_snapshot()
        self.assertEqual(calls["load"], 1)
        self.assertEqual(calls["compute"], 1)
        got = df.set_index("symbol")
        self.assertAlmostEqual(float(got.loc["600000.SH", "close"]), 11.0)
        self.assertAlmostEqual(float(got.loc["000001.SZ", "close"]), 12.0,
                               msg="无当日快照的标的应沿用收盘因子")
        self.assertIn("+live@", ver)


class YjbbPeriodTest(unittest.TestCase):
    def test_candidates_skip_unfinished_quarter(self):
        # 2026-02-15: 当年各季都没结束, 最新候选是上年年报而不是当年 0930
        self.assertEqual(factors._yjbb_candidates("2026-02-15")[0], "20251231")
        self.assertNotIn("20260930", factors._yjbb_candidates("2026-02-15"))
        self.assertEqual(factors._yjbb_candidates("2026-09-18")[0], "20260630")

    def test_thin_newer_period_falls_through(self):
        def fake(date):
            if date == "20251231":
                return pd.DataFrame([{
                    "股票代码": "600000", "净资产收益率": 1.0,
                    "所处行业": "银行", "每股收益": 1.0, "每股净资产": 1.0,
                }])
            if date == "20250930":
                rows = [{"股票代码": f"{i:06d}", "净资产收益率": 2.0,
                         "所处行业": "银行", "每股收益": 1.0, "每股净资产": 1.0}
                        for i in range(factors.YJBB_MIN_ROWS)]
                return pd.DataFrame(rows)
            raise RuntimeError(date)

        with mock.patch("akshare.stock_yjbb_em", side_effect=fake):
            out = factors._src_yjbb("2026-02-15")
        self.assertEqual(out["_period"], "20250930")
        self.assertGreaterEqual(len(out["rows"]), factors.YJBB_MIN_ROWS)


class StreamingBuildTest(StoreTestCaseBase):
    """流式构建: 按批处理与整份一批的结果必须一致, 缺失口径不变。"""

    UNI = [("600000.SH", "浦发"), ("000001.SZ", "平安"), ("600519.SH", "茅台"),
           ("000002.SZ", "万科")]

    def _frames(self, day="2026-09-18"):
        return {
            "600000.SH": _df([10.0 + 0.5 * i for i in range(120)], end=day),
            "000001.SZ": _df([70.0 - 0.3 * i for i in range(120)], end=day),
            "600519.SH": _df([100.0 + i for i in range(120)], end="2026-09-17"),  # 停牌
            # 000002.SZ 整批没拉到
        }

    def _build(self, size):
        factors.init_store()
        with mock.patch.object(factors, "universe", lambda: list(self.UNI)), \
             mock.patch.object(factors, "fetch_bars", _gen_bars(self._frames(), size=size)), \
             mock.patch.object(factors, "_fetch_shares", lambda syms: {}), \
             mock.patch.object(factors, "external_meta", lambda day, syms: ({}, {})), \
             mock.patch.object(factors, "_notify_state", lambda *a, **k: None):
            out = factors.build(day="2026-09-18", force=True)
        df, _day = factors.snapshot()
        return out, df.sort_values("symbol").reset_index(drop=True)

    def test_batched_equals_single_batch(self):
        out_one, df_one = self._build(size=None)
        self._restore()
        self.setUp()
        out_many, df_many = self._build(size=1)
        self.assertTrue(out_one["ok"] and out_many["ok"])
        pd.testing.assert_frame_equal(df_one, df_many)
        self.assertEqual(out_one["n_missing"], out_many["n_missing"])

    def test_missing_counts_unfetched_and_suspended(self):
        out, df = self._build(size=1)
        self.assertEqual(sorted(df["symbol"]), ["000001.SZ", "600000.SH"])
        self.assertEqual(out["n_missing"], 2, "停牌 1 只 + 没拉到 1 只")
        self.assertEqual(len(factors.load_bars("600519.SH")), 120, "停牌标的的 K 线照样落库")

    def test_real_fetch_bars_yields_per_chunk(self):
        seen = []

        def chunk(af, syms, count):
            seen.append(list(syms))
            return {s: _df([10.0] * 40) for s in syms}

        with mock.patch.object(factors, "BATCH_SIZE", 2), \
             mock.patch.object(factors, "_fetch_bars_chunk", chunk), \
             mock.patch("market.get_af", return_value=object()), \
             mock.patch("market._normalize", lambda df, prefer_time=False: df):
            gen = factors.fetch_bars(["A", "B", "C"])
            first, stats = next(gen)
            self.assertEqual(sorted(first), ["A", "B"])
            self.assertEqual(len(seen), 1, "第二批要等调用方消费完第一批才拉")
            rest = list(gen)
        self.assertEqual(sorted(rest[0][0]), ["C"])
        self.assertEqual(rest[-1][1]["done"], 2)
        self.assertEqual(rest[-1][1]["batches"], 2)

    def test_partial_batch_commits_before_next_progress_write(self):
        """一批只有 1 只成功也要提交，否则下一批进度写入会被自身写锁挡住。"""
        factors.init_store()
        day = "2026-09-18"
        factors._ensure_build_row(day)
        bar = _df([10.0] * 40, end=day)

        def fetch(_symbols, progress_cb=None):
            yield {"600000.SH": bar}, {"batches": 2, "done": 1, "failed_batches": 0}
            # 模拟 fetch_bars 拉下一批时的进度回调；立刻失败可避免 8 秒测试等待。
            import sqlite3
            with sqlite3.connect(str(factors.DB_PATH), timeout=0) as conn:
                conn.execute("UPDATE builds SET percent=50 WHERE day=?", (day,))
            yield {"000001.SZ": bar}, {"batches": 2, "done": 2, "failed_batches": 0}

        with mock.patch.object(factors, "universe", lambda: [("600000.SH", "浦发"),
                                                              ("000001.SZ", "平安")]), \
             mock.patch.object(factors, "fetch_bars", fetch), \
             mock.patch.object(factors, "_fetch_shares", lambda _symbols: {}), \
             mock.patch.object(factors, "external_meta", lambda _day, _symbols: ({}, {})):
            result = factors._build_inner(day)
        self.assertEqual(result["n_symbols"], 2)


class TokenWaitTest(unittest.TestCase):
    def test_sleeps_by_refill_rate_not_busy_loop(self):
        bucket = mock.Mock(rate=0.5)
        bucket.try_acquire.side_effect = [False, False, True]
        with mock.patch.object(factors.time, "sleep") as sleep:
            factors._acquire_token(bucket)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 2.0])

    def test_non_numeric_rate_falls_back(self):
        bucket = mock.Mock()
        bucket.try_acquire.side_effect = [False, True]
        with mock.patch.object(factors.time, "sleep") as sleep:
            factors._acquire_token(bucket)
        sleep.assert_called_once_with(1.0)


class BuildChildTest(StoreTestCaseBase):
    def setUp(self):
        super().setUp()
        factors._build_children.clear()
        self.addCleanup(factors._build_children.clear)

    def test_spawn_passes_force_and_tracks_child(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        with mock.patch("subprocess.Popen", return_value=proc) as popen:
            factors._spawn_build("2026-09-18", force=True)
        self.assertIn("--force", popen.call_args.args[0])
        self.assertTrue(factors.build_child_running())

    def test_spawn_rejects_non_canonical_day_before_popen(self):
        with mock.patch("subprocess.Popen") as popen:
            for bad in ("2026-09-18; rm -rf /", "2026/09/18", "nope", ""):
                with self.assertRaises(ValueError):
                    factors._spawn_build(bad, force=True)
        popen.assert_not_called()

    def test_spawn_manual_uses_subprocess(self):
        with mock.patch.object(factors, "_spawn_build") as spawn:
            factors.spawn_manual(day="2026-09-18", force=True)
        spawn.assert_called_once_with("2026-09-18", force=True)

    def test_second_spawn_is_refused_before_popen(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        with mock.patch("subprocess.Popen", return_value=proc) as popen:
            factors._spawn_build("2026-09-18")
            with self.assertRaises(factors.BuildAlreadyRunning):
                factors._spawn_build("2026-09-18", force=True)
        popen.assert_called_once()

    def test_running_child_blocks_manual_rebuild_even_with_force(self):
        factors.init_store()
        proc = mock.Mock()
        proc.poll.return_value = None
        factors._track_child(proc, "2026-09-18")
        reason = factors.build_blocked_reason(day="2026-09-18", force=True)
        self.assertIn("构建", reason)

    def test_recent_running_build_row_blocks_after_web_restart(self):
        factors.init_store()
        factors._ensure_build_row("2026-09-18")
        factors._update_build("2026-09-18", state="running", started_at=factors._iso())
        reason = factors.build_blocked_reason(day="2026-09-18", force=True)
        self.assertIn("构建", reason)

    def test_stop_children_terminates_and_marks_error(self):
        factors.init_store()
        factors._ensure_build_row("2026-09-18")
        factors._update_build("2026-09-18", state="running")
        proc = mock.Mock()
        proc.poll.return_value = None
        factors._track_child(proc, "2026-09-18")
        self.assertEqual(factors.stop_build_children(), 1)
        proc.terminate.assert_called_once()
        row = factors.get_build("2026-09-18")
        self.assertEqual(row["state"], "error", "被终止的构建不能一直显示 running")
        self.assertIn("内存", row["error"])

    def test_finished_child_not_terminated(self):
        proc = mock.Mock()
        proc.poll.return_value = 0
        factors._track_child(proc, "2026-09-18")
        self.assertEqual(factors.stop_build_children(), 0)
        proc.terminate.assert_not_called()

    def test_scheduler_skips_spawn_under_pressure(self):
        sleeps = {"n": 0}

        def fake_sleep(_sec):
            sleeps["n"] += 1
            if sleeps["n"] >= 1:
                raise SystemExit

        with mock.patch.object(factors, "init_store", lambda: None), \
             mock.patch.object(factors, "should_build", lambda now=None: (True, "due")), \
             mock.patch.object(factors, "due_day", lambda now=None: "2026-09-18"), \
             mock.patch.object(factors, "_under_pressure", lambda: True), \
             mock.patch.object(factors, "_spawn_build") as spawn, \
             mock.patch("time.sleep", fake_sleep):
            with self.assertRaises(SystemExit):
                factors._loop()
        spawn.assert_not_called()


@unittest.skipIf(sys.platform == "win32", "flock 仅 POSIX")
class HeavyLockTest(StoreTestCaseBase):
    def test_second_holder_is_refused_until_release(self):
        import threading
        self.assertTrue(factors.heavy_lock.acquire(blocking=False))
        got = []
        t = threading.Thread(target=lambda: got.append(factors.heavy_lock.acquire(blocking=False)))
        t.start()
        t.join()
        self.assertEqual(got, [False], "另一持有者必须拿不到")
        factors.heavy_lock.release()
        self.assertTrue(factors.heavy_lock.acquire(blocking=False))
        factors.heavy_lock.release()


class LiveSnapshotGuardTest(unittest.TestCase):
    BASE = pd.DataFrame([
        {"symbol": "600000.SH", "name": "浦发", "close": 10.0, "prev_close": 9.5},
        {"symbol": "000001.SZ", "name": "平安", "close": 12.0, "prev_close": 12.0},
        {"symbol": "600519.SH", "name": "茅台", "close": 100.0, "prev_close": 99.0},
    ])
    QUOTES = {s: {"last_price": p, "volume": 100, "amount": 100 * p,
                  "high": p, "low": p, "open": p}
              for s, p in (("600000.SH", 11.0), ("000001.SZ", 12.5), ("600519.SH", 101.0))}

    def setUp(self):
        factors._live_cache.update({"day": None, "ts": 0.0, "df": None, "version": None})

    def _run(self, chunk, loads, **extra):
        def load_many(symbols):
            loads.append(list(symbols))
            return {s: _df([10.0] * 40, end="2026-09-17") for s in symbols}

        def compute(df, symbol="", name="", float_shares=None):
            return {"symbol": symbol, "close": float(df["close"].iloc[-1])}

        patches = [
            mock.patch.object(factors, "snapshot", lambda day=None: (self.BASE, "2026-09-18")),
            mock.patch.object(factors, "_now_dt", lambda: datetime(2026, 9, 18, 10, 30)),
            mock.patch("market_hours.session_phase", lambda now=None: "trading"),
            mock.patch.object(factors, "_fetch_quotes", lambda syms, progress_cb=None: self.QUOTES),
            mock.patch.object(factors, "load_bars_many", load_many),
            mock.patch.object(factors, "load_shares", lambda: {}),
            mock.patch.object(factors, "compute_factors", compute),
            mock.patch.dict("os.environ", {"FACTORS_LIVE_CHUNK": str(chunk)}),
        ]
        for key, value in extra.items():
            patches.append(mock.patch.object(factors, key, value))
        for p in patches:
            p.start()
        try:
            return factors.live_snapshot()
        finally:
            for p in reversed(patches):
                p.stop()

    def test_chunked_equals_single_read(self):
        one, many = [], []
        df_one, _ = self._run(1000, one)
        factors._live_cache.update({"day": None, "ts": 0.0, "df": None, "version": None})
        df_many, _ = self._run(1, many)
        self.assertEqual(len(one), 1)
        self.assertEqual(len(many), 3, "分块后每块单独读 K 线")
        pd.testing.assert_frame_equal(df_one.reset_index(drop=True), df_many.reset_index(drop=True))

    def test_skips_when_heavy_lock_busy(self):
        loads = []
        busy = mock.Mock()
        busy.acquire.return_value = False
        df, ver = self._run(300, loads, heavy_lock=busy)
        self.assertEqual((df, ver), (None, None))
        self.assertEqual(loads, [])
        busy.release.assert_not_called()

    def test_skips_under_memory_pressure(self):
        loads = []
        df, ver = self._run(300, loads, _under_pressure=lambda: True)
        self.assertEqual((df, ver), (None, None))
        self.assertEqual(loads, [])
        self.assertFalse(factors.live_running())


class MainJsonTest(StoreTestCaseBase):
    def test_status_and_build_both_dump_json(self):
        factors.init_store()
        buf = []
        with mock.patch("sys.argv", ["factors.py", "--status"]), \
             mock.patch("builtins.print", lambda s: buf.append(s)):
            factors.main()
        self.assertIn("snapshot_day", buf[-1])
        buf.clear()
        with mock.patch.object(factors, "build", return_value={"ok": True}), \
             mock.patch("sys.argv", ["factors.py", "--build", "--day", "2026-09-18"]), \
             mock.patch("builtins.print", lambda s: buf.append(s)):
            factors.main()
        self.assertIn('"ok": true', buf[-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
