# -*- coding: utf-8 -*-
"""图表日K预热仓: 列式读写、读路径跳过源链、昨收漂移、全量封顶、指数软失败。

不碰生产 out_files/daily_qfq。运行:
    venv/Scripts/python.exe -u visual/test/test_daily_chart_store.py
"""
from __future__ import annotations

import gc
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

try:
    import psutil
except ImportError:  # 部署 venv 未必装 psutil
    psutil = None

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import build_daily_chart_cache as chart_build
import daily_chart_store as store
import market


def _frame(closes, start="2026-01-05"):
    closes = [float(value) for value in closes]
    index = pd.bdate_range(start, periods=len(closes))
    close = pd.Series(closes, index=index, dtype=float)
    return pd.DataFrame({
        "open": close * 0.99,
        "high": close * 1.01,
        "low": close * 0.98,
        "close": close,
        "volume": 1000.0,
        "amount": close * 1000.0,
    }, index=index)


class ChartStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store.relocate(Path(self.tmp.name))
        self.addCleanup(lambda: store.relocate(None))
        store._dirty.clear()
        store._cooled.clear()
        store._fail_counts.clear()
        store._cooled_day = None
        patcher = mock.patch.object(store, "required_as_of", return_value="2026-11-20")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fetch(self, period, count, append=None, **kwargs):
        append = append or (lambda symbol, df, **_k: df)
        with mock.patch.object(market, "_maybe_append_today_bar", side_effect=append), \
             mock.patch.object(market, "_lookup_name", return_value="浦发"), \
             mock.patch.object(market.kline_source, "fetch_kline_df",
                               return_value=(None, None)) as fetch:
            out = market.fetch_kline_ex("600000.SH", period, count, adjust="forward", **kwargs)
        return out, fetch

    def test_roundtrip_and_generation_switch(self):
        store.publish_frames(
            {"600000.SH": _frame([10, 11, 12, 13, 14, 15])},
            count=20, as_of="2026-11-20",
        )
        first = store.daily_frame("600000.SH")
        self.assertAlmostEqual(float(first["close"].iloc[-1]), 15.0)
        self.assertGreater(store.status()["bytes"], 0)

        store.publish_frames(
            {"600000.SH": _frame([10, 11, 12, 13, 14, 21])},
            count=20, as_of="2026-11-21",
        )
        second = store.daily_frame("600000.SH")
        self.assertAlmostEqual(float(second["close"].iloc[-1]), 21.0)
        self.assertAlmostEqual(float(first["close"].iloc[-1]), 15.0)
        gens = [path.name for path in Path(self.tmp.name).iterdir() if path.is_dir()]
        self.assertEqual(len(gens), 1)

    def test_patch_replaces_one_symbol(self):
        store.publish_frames({
            "600000.SH": _frame([10, 11, 12, 13, 14, 15]),
            "000001.SZ": _frame([20, 21, 22, 23, 24, 25]),
        }, count=20, as_of="2026-11-20")
        gen_before = store.open_store().path
        changed = store.patch_frames({"600000.SH": _frame([10, 11, 12, 13, 14, 30])})
        self.assertEqual(changed, 1)
        self.assertEqual(store.open_store().path, gen_before)
        self.assertAlmostEqual(float(store.daily_frame("600000.SH")["close"].iloc[-1]), 30.0)
        self.assertAlmostEqual(float(store.daily_frame("000001.SZ")["close"].iloc[-1]), 25.0)
        self.assertEqual(store.patch_frames({"688001.SH": _frame([1, 2, 3, 4, 5, 6])}), 0)

        store.publish_frames({"600000.SH": _frame([10, 11, 12, 13, 14, 15])},
                             count=20, as_of="2026-11-21")
        self.assertAlmostEqual(float(store.daily_frame("600000.SH")["close"].iloc[-1]), 15.0)

    def test_patch_drops_bars_after_as_of(self):
        store.publish_frames({"600000.SH": _frame(range(1, 11))}, count=20, as_of="2026-01-14")
        store.patch_frames({"600000.SH": _frame(range(101, 111))})
        frame = store.daily_frame("600000.SH")
        self.assertEqual(frame.index[-1], pd.Timestamp("2026-01-14"))
        self.assertAlmostEqual(float(frame["close"].iloc[-1]), 108.0)

    def test_index_miss_keeps_stocks(self):
        def pull(symbols):
            if symbols and symbols[0].endswith(".SH") and symbols[0].startswith("000"):
                raise RuntimeError("index down")
            return {symbol: _frame([1, 2, 3, 4, 5, 6]) for symbol in symbols}

        frames, err = chart_build.collect_frames(pull, ["600000.SH"], ["000001.SH"])
        self.assertIn("600000.SH", frames)
        self.assertNotIn("000001.SH", frames)
        self.assertIn("index down", err)
        manifest = store.publish_frames(
            frames, count=20, as_of="2026-11-20",
            index_symbols={"000001.SH"}, index_error=err,
        )
        self.assertEqual(manifest["index_count"], 0)
        self.assertGreater(manifest["symbols_with_data"], 0)
        self.assertEqual(manifest["role"], "visual_chart")

    def test_full_pull_cap(self):
        store.note_full_pull("2026-11-23")
        store.note_full_pull("2026-11-23")
        self.assertEqual(store.pulls_today("2026-11-23"), 2)
        self.assertIn("2", store.full_pull_block_reason("2026-11-23", force=True))
        self.assertEqual(store.pulls_today("2026-11-24"), 0)
        with mock.patch("market_hours.now", return_value=datetime(2026, 11, 24, 10, 0)):
            self.assertEqual(store.status()["full_pulls_today"], 0)
            self.assertEqual(store.status()["full_pulls_day"], "2026-11-23")

    def test_drift_enqueues_then_cools(self):
        store.observe_alignment("600000.SH", 10.0, prev_close=10.01)
        self.assertEqual(store.dirty_symbols(), [])
        store.observe_alignment("600000.SH", 10.0, prev_close=9.0)
        self.assertEqual(store.dirty_symbols(), ["600000.SH"])
        store.mark_cooled(["600000.SH"])
        store.observe_alignment("600000.SH", 10.0, prev_close=8.0)
        self.assertEqual(store.dirty_symbols(), [])

    def test_factor_batches_slice_or_miss(self):
        store.publish_frames(
            {"600000.SH": _frame(range(1, 41))},
            count=40, as_of="2026-11-20",
        )
        missed = store.factor_batches(["600000.SH"], 10, "2026-11-21")
        self.assertIsNone(missed)
        found = list(store.factor_batches(["600000.SH", "000001.SZ"], 10, "2026-11-20"))
        self.assertEqual(len(found), 1)
        batch, stats = found[0]
        self.assertEqual(list(batch), ["600000.SH"])
        self.assertEqual(len(batch["600000.SH"]), 10)
        self.assertEqual(stats["source"], "daily_chart")
        self.assertEqual(float(batch["600000.SH"]["close"].iloc[-1]), 40.0)

    def test_chart_hit_skips_source_chain(self):
        store.publish_frames(
            {"600000.SH": _frame(range(10, 50))},
            count=60, as_of="2026-11-20",
        )
        (df, name, source), fetch = self._fetch("1d", 100)
        fetch.assert_not_called()
        self.assertEqual(source, "daily_chart")
        self.assertEqual(name, "浦发")
        self.assertEqual(len(df), 40)
        self.assertAlmostEqual(float(df["close"].iloc[-1]), 49.0)

        with mock.patch.object(market, "_maybe_append_today_bar", side_effect=lambda symbol, df, **_k: df), \
             mock.patch.object(market.kline_source, "fetch_kline_df", return_value=(None, None)) as fetch:
            market.fetch_kline_ex("600000.SH", "1d", 100, adjust="none")
        fetch.assert_called()

    def test_stale_store_falls_back(self):
        store.publish_frames({"600000.SH": _frame(range(10, 50))}, count=60, as_of="2026-11-19")
        _out, fetch = self._fetch("1d", 30)
        fetch.assert_called()

    def test_truncated_store_falls_back_when_count_not_covered(self):
        store.publish_frames({"600000.SH": _frame(range(10, 50))}, count=40, as_of="2026-11-20")
        (_df, _name, source), fetch = self._fetch("1d", 30)
        fetch.assert_not_called()
        self.assertEqual(source, "daily_chart")
        _out, fetch = self._fetch("1d", 100)
        fetch.assert_called()
        # 周/月仓内根数不够前端 count 时仍用重采样结果，避免几乎永远 miss
        (weekly, _name, source), fetch = self._fetch("1w", 30)
        fetch.assert_not_called()
        self.assertEqual(source, "daily_chart")
        self.assertGreaterEqual(len(weekly), 5)
        self.assertLess(len(weekly), 30)

    def test_weekly_resample_from_store(self):
        store.publish_frames(
            {"600000.SH": _frame(range(1, 41))},
            count=60, as_of="2026-11-20",
        )
        (df, _name, source), fetch = self._fetch("1w", 30)
        fetch.assert_not_called()
        self.assertEqual(source, "daily_chart")
        self.assertGreaterEqual(len(df), 5)
        self.assertAlmostEqual(float(df["close"].iloc[-1]), 40.0)

    def test_resample_labels_last_trading_day(self):
        frame = _frame(range(1, 16), start="2026-11-02")   # 11-02 … 11-20, 周一至周五
        frame = frame.drop(pd.Timestamp("2026-11-13"))      # 那周周五休市
        frame = frame[frame.index <= pd.Timestamp("2026-11-18")]  # 本周只到周三
        weekly = store.resample_bars(frame, "1w")
        self.assertEqual([day.date().isoformat() for day in weekly.index],
                         ["2026-11-06", "2026-11-12", "2026-11-18"])
        daily = _frame(range(1, 30), start="2026-10-01")
        monthly = store.resample_bars(daily, "1M")
        self.assertEqual(monthly.index[0], pd.Timestamp("2026-10-30"))
        self.assertEqual(monthly.index[-1], daily.index[-1])

    def test_alignment_only_after_quote_bar_applied(self):
        store.publish_frames({"600000.SH": _frame(range(10, 50))}, count=60, as_of="2026-11-20")
        with mock.patch.object(store, "observe_alignment") as observe:
            self._fetch("1d", 30)
        observe.assert_not_called()

        def applied(symbol, df, **_k):
            df = df.copy()
            df.attrs["today_quote_meta"] = {"phase": "trading"}
            return df

        with mock.patch("market_hours.session_phase", return_value="trading"), \
             mock.patch.object(store, "observe_alignment") as observe:
            self._fetch("1d", 30, append=applied)
        observe.assert_called_once()

    def test_closed_session_skips_quote_when_today_bar_present(self):
        # 末根落到「今天」，收盘相位下图表仓不再去抢快照
        index = pd.bdate_range(end=pd.Timestamp("2026-11-23"), periods=20)
        close = pd.Series(range(1, 21), index=index, dtype=float)
        frame = pd.DataFrame({
            "open": close * 0.99, "high": close * 1.01, "low": close * 0.98,
            "close": close, "volume": 1000.0, "amount": close * 1000.0,
        }, index=index)
        store.publish_frames({"600000.SH": frame}, count=40, as_of="2026-11-23")
        append = mock.Mock(side_effect=lambda symbol, df, **_k: df)
        with mock.patch("market_hours.session_phase", return_value="closed"), \
             mock.patch("market_hours.now", return_value=datetime(2026, 11, 23, 20, 0)), \
             mock.patch.object(store, "required_as_of", return_value="2026-11-20"), \
             mock.patch.object(market, "_maybe_append_today_bar", append), \
             mock.patch.object(market, "_lookup_name", return_value="浦发"), \
             mock.patch.object(market.kline_source, "fetch_kline_df") as fetch:
            df, _name, source = market.fetch_kline_ex("600000.SH", "1d", 30, adjust="forward")
        fetch.assert_not_called()
        append.assert_not_called()
        self.assertEqual(source, "daily_chart")
        self.assertEqual(len(df), 20)

    def test_schedule_catch_up_wait_and_backoff(self):
        with mock.patch.object(chart_build, "target_day", return_value="2026-11-23"):
            ok, why = chart_build.should_build(datetime(2026, 11, 23, 15, 30))
            self.assertFalse(ok)
            self.assertIn("16:05", why)
            self.assertEqual(chart_build.should_build(datetime(2026, 11, 23, 16, 6)), (True, "due"))
        with mock.patch.object(chart_build, "target_day", return_value="2026-11-20"):
            ok, why = chart_build.should_build(datetime(2026, 11, 23, 9, 40))
            self.assertFalse(ok)
            self.assertIn("盘中", why)
            self.assertEqual(chart_build.should_build(datetime(2026, 11, 23, 15, 30)), (True, "due"))
            self.assertEqual(chart_build.should_build(datetime(2026, 11, 22, 10, 0)), (True, "due"))
            failed = datetime(2026, 11, 23, 15, 20)
            store.update_meta(error="boom", finished_at=failed.isoformat())
            ok, why = chart_build.should_build(datetime(2026, 11, 23, 15, 30))
            self.assertFalse(ok)
            self.assertIn("30", why)
            self.assertEqual(chart_build.should_build(datetime(2026, 11, 23, 16, 0)), (True, "due"))
            store.publish_frames({"600000.SH": _frame(range(1, 11))}, count=20, as_of="2026-11-20")
            ok, _why = chart_build.should_build(datetime(2026, 11, 23, 16, 0))
            self.assertFalse(ok)

    def test_session_blocks_unless_force(self):
        with mock.patch.object(chart_build, "target_day", return_value="2026-11-20"):
            mid = datetime(2026, 11, 23, 10, 0)
            self.assertIn("盘中", chart_build._block_reason(mid, force=False) or "")
            self.assertIsNone(chart_build._block_reason(mid, force=True))

    def test_failed_full_does_not_count(self):
        with mock.patch.object(chart_build, "_now", return_value=datetime(2026, 11, 23, 16, 10)), \
             mock.patch.object(chart_build, "target_day", return_value="2026-11-23"), \
             mock.patch.object(chart_build, "universe", return_value=(["600000.SH"], [])), \
             mock.patch.object(chart_build, "_pull", side_effect=RuntimeError("no key")):
            out = chart_build.run_full(force=False)
        self.assertFalse(out.get("ok"))
        self.assertIn("no key", out.get("error", ""))
        self.assertEqual(store.pulls_today("2026-11-23"), 0)
        self.assertIsNone(store.full_pull_block_reason("2026-11-23", force=True))
        self.assertEqual(store.failed_today("2026-11-23"), 1)

    def test_failures_stop_auto_retry_but_not_force(self):
        evening = datetime(2026, 11, 23, 20, 0)
        for _ in range(chart_build.MAX_FAILED_PULLS):
            store.note_failed_pull("2026-11-23")
        with mock.patch.object(chart_build, "target_day", return_value="2026-11-23"):
            self.assertIn("失败", chart_build._block_reason(evening, force=False) or "")
            self.assertIsNone(chart_build._block_reason(evening, force=True))

    def test_pre_open_guard(self):
        with mock.patch.object(chart_build, "target_day", return_value="2026-11-20"):
            self.assertIsNone(chart_build._block_reason(datetime(2026, 11, 23, 8, 50), force=False))
            self.assertIn("盘中", chart_build._block_reason(datetime(2026, 11, 23, 9, 6), force=False) or "")

    def test_cut_to_day_drops_partial_bar(self):
        frames = chart_build.cut_to_day({"600000.SH": _frame(range(1, 11))}, "2026-01-14")
        self.assertEqual(frames["600000.SH"].index[-1], pd.Timestamp("2026-01-14"))
        self.assertEqual(chart_build.cut_to_day({"600000.SH": _frame([1, 2])}, "2025-12-31"), {})

    def test_short_count_does_not_use_store(self):
        store.publish_frames(
            {"600000.SH": _frame(range(1, 11))},
            count=20, as_of="2026-11-20",
        )
        with mock.patch.object(market.kline_source, "fetch_kline_df", return_value=(None, None)) as fetch:
            market.fetch_kline_ex("600000.SH", "1d", 1)
        fetch.assert_called()

    def test_startup_refresh_spawns_when_stale(self):
        with mock.patch.object(chart_build, "should_build", return_value=(True, "due")), \
             mock.patch.object(chart_build, "spawn_build") as spawn, \
             mock.patch.object(store, "open_store", return_value=None):
            out = chart_build.startup_refresh()
        spawn.assert_called_once_with(force=False)
        self.assertTrue(out["spawned"])
        self.assertEqual(out["reason"], "due")
        # 不过时: 只预热, 不拉起全量
        with mock.patch.object(chart_build, "should_build", return_value=(False, "已是最新")), \
             mock.patch.object(chart_build, "spawn_build") as spawn, \
             mock.patch.object(store, "open_store", return_value=mock.Mock()):
            out = chart_build.startup_refresh()
        spawn.assert_not_called()
        self.assertFalse(out["spawned"])
        self.assertEqual(out["reason"], "已是最新")

    def test_chart_ohlc_matches_uncached_source(self):
        """同一份日K: 走预热仓与 DAILY_CHART_DISABLE 源链, OHLC 一致。"""
        raw = _frame(range(10, 50))
        store.publish_frames({"600000.SH": raw}, count=60, as_of="2026-11-20")
        (chart_df, _n, src), fetch = self._fetch("1d", 40)
        fetch.assert_not_called()
        self.assertEqual(src, "daily_chart")

        with mock.patch.dict(os.environ, {"DAILY_CHART_DISABLE": "1"}), \
             mock.patch.object(market, "_disk_cache") as disk, \
             mock.patch.object(market, "_maybe_append_today_bar", side_effect=lambda s, d, **_k: d), \
             mock.patch.object(market, "_lookup_name", return_value="浦发"), \
             mock.patch.object(market.kline_source, "fetch_kline_df",
                               return_value=(raw.copy(), "alphafeed")) as fetch:
            disk.get.return_value = None
            src_df, _n2, src2 = market.fetch_kline_ex(
                "600000.SH", "1d", 40, adjust="forward")
        fetch.assert_called()
        self.assertEqual(src2, "alphafeed")
        self.assertEqual(len(chart_df), len(src_df))
        for col in ("open", "high", "low", "close", "volume"):
            np.testing.assert_allclose(
                chart_df[col].to_numpy(dtype=float),
                src_df[col].to_numpy(dtype=float),
                rtol=1e-5, atol=1e-5, err_msg=col)

    def test_weekly_chart_matches_resample_of_uncached_daily(self):
        raw = _frame(range(1, 41))
        store.publish_frames({"600000.SH": raw}, count=60, as_of="2026-11-20")
        (week_df, _n, src), fetch = self._fetch("1w", 30)
        fetch.assert_not_called()
        self.assertEqual(src, "daily_chart")
        expect = store.resample_bars(raw, "1w").tail(30)
        self.assertEqual(list(week_df.index), list(expect.index))
        np.testing.assert_allclose(
            week_df["close"].to_numpy(dtype=float),
            expect["close"].to_numpy(dtype=float), rtol=1e-5, atol=1e-5)

    def test_disable_env_falls_back_like_before(self):
        store.publish_frames({"600000.SH": _frame(range(10, 50))}, count=60, as_of="2026-11-20")
        with mock.patch.dict(os.environ, {"DAILY_CHART_DISABLE": "1"}), \
             mock.patch.object(market, "_disk_cache") as disk, \
             mock.patch.object(market, "_maybe_append_today_bar", side_effect=lambda s, d, **_k: d), \
             mock.patch.object(market, "_lookup_name", return_value="浦发"), \
             mock.patch.object(market.kline_source, "fetch_kline_df",
                               return_value=(_frame(range(10, 50)), "alphafeed")) as fetch:
            disk.get.return_value = None
            _df, _n, src = market.fetch_kline_ex("600000.SH", "1d", 40, adjust="forward")
        fetch.assert_called()
        self.assertEqual(src, "alphafeed")

    @unittest.skipIf(psutil is None, "需要 psutil 读 RSS")
    def test_mmap_rss_far_below_file_size(self):
        """列式 mmap: 打开 + 点读若干票后, RSS 增量应远小于文件体积。"""
        n_sym, n_bars = 250, 400
        template = _frame(range(1, n_bars + 1))
        frames = {f"{i:06d}.SZ": template for i in range(n_sym)}
        store.publish_frames(frames, count=n_bars, as_of="2026-11-20")
        store._release_reader()
        gc.collect()

        proc = psutil.Process()
        before = proc.memory_info().rss
        nbytes = store.disk_bytes()
        self.assertGreater(nbytes, 2_000_000)

        opened = store.open_store()
        self.assertIsNotNone(opened)
        after_open = proc.memory_info().rss
        for i in range(25):
            frame = store.daily_frame(f"{i:06d}.SZ")
            self.assertIsNotNone(frame)
            self.assertEqual(len(frame), n_bars)
        after_read = proc.memory_info().rss

        open_delta = after_open - before
        read_delta = after_read - before
        # 允许 page cache / 分配器噪声, 但仍须明显小于整仓体积
        self.assertLess(open_delta, max(12_000_000, int(nbytes * 0.35)),
                        f"open RSS +{open_delta} vs file {nbytes}")
        self.assertLess(read_delta, max(20_000_000, int(nbytes * 0.50)),
                        f"read RSS +{read_delta} vs file {nbytes}")


class KlineApiChartPathTest(unittest.TestCase):
    """ /api/kline 热路径: 有仓时不堵 instruments / 盘后不堵 quote。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store.relocate(Path(self.tmp.name))
        self.addCleanup(lambda: store.relocate(None))
        patcher = mock.patch.object(store, "required_as_of", return_value="2026-11-20")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_kline_body_skips_blocking_instrument_fetch(self):
        from app import create_app
        from api import kline as kline_api

        raw = _frame(range(1, 61))
        store.publish_frames({"600000.SH": raw}, count=80, as_of="2026-11-20")
        market._instrument_meta_cache.clear()

        app = create_app()
        with app.test_request_context(
                "/api/kline?symbol=600000.SH&period=1d&count=40&adjust=forward"):
            with mock.patch.object(kline_api, "fetch_kline_ex",
                                   return_value=(raw.tail(40).copy(), "浦发", "daily_chart")), \
                 mock.patch.object(kline_api, "_is_etf", return_value=False), \
                 mock.patch.object(kline_api, "_is_index_symbol", return_value=False), \
                 mock.patch.object(market, "_fetch_instrument_meta") as meta_fetch, \
                 mock.patch.object(kline_api, "_fetch_instrument_meta") as api_meta_fetch, \
                 mock.patch.object(kline_api, "fetch_quote") as quote_fetch, \
                 mock.patch("market_hours.is_live", return_value=False):
                resp = kline_api._kline_body(None)
        self.assertEqual(resp.status_code, 200)
        meta_fetch.assert_not_called()
        api_meta_fetch.assert_not_called()
        quote_fetch.assert_not_called()
        body = resp.get_json()
        self.assertEqual(body["meta"]["source"], "daily_chart")
        self.assertEqual(len(body["klines"]), 40)


if __name__ == "__main__":
    unittest.main()
