"""信息栏估值: 按股 30 分钟缓存、交易时段门禁、落盘及失败保留旧值。"""
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import market


def _snapshot(pe, pb, day="2026-09-18"):
    return {"pe": pe, "pb": pb, "pe_source": "snapshot", "pb_source": "snapshot",
            "pe_date": day, "pb_date": day}


class StockValuationTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.clock = datetime(2026, 9, 18, 10, 0)
        self.values = {"pe": 25.33, "pb_ratio": 2.89}
        self.mr = mock.Mock()
        self.mr.stock_ssjy_more.side_effect = lambda codes: [{"dm": codes[0], **self.values}]
        self.mr.hsdc_himk_syl.return_value = []
        self.mr.hsdc_himk_sjl.return_value = []
        self.budget = mock.Mock()
        self.budget.acquire.return_value = True
        self.budget.interval = 3.0
        patches = [
            mock.patch.object(market, "_MR_VALUATION_DIR", self.root),
            mock.patch.object(market, "_MR_VALUATION_RANK_DIR", self.root / "rankings"),
            mock.patch.object(market, "_mr_valuation_cache", {}),
            mock.patch.object(market, "_mr_valuation_rank_cache", {}),
            mock.patch.object(market, "MAIRUI_API_KEY", "TESTKEY"),
            mock.patch.object(market, "get_mr", return_value=self.mr),
            mock.patch.object(market, "_mr_quote_budget", self.budget),
            mock.patch.object(market.market_hours, "now", side_effect=lambda: self.clock),
            mock.patch.object(market.market_hours, "is_trading_day",
                              side_effect=lambda day: day.weekday() < 5),
            mock.patch.object(market.time, "time", side_effect=self._epoch),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _epoch(self):
        return self.clock.replace(tzinfo=timezone(timedelta(hours=8))).timestamp()

    def test_refreshes_at_30_minutes_only_when_queried(self):
        first = market._mr_stock_valuation("002472")
        self.values = {"pe": 26.0, "pb_ratio": 3.0}
        self.clock += timedelta(seconds=1799)
        self.assertEqual(market._mr_stock_valuation("002472"), first)
        self.mr.stock_ssjy_more.assert_called_once_with(["002472"])
        self.clock += timedelta(seconds=1)
        self.assertEqual(market._mr_stock_valuation("002472"), _snapshot(26.0, 3.0))
        self.assertEqual(self.mr.stock_ssjy_more.call_count, 2)

    def test_off_session_retains_cache_without_fetching(self):
        first = market._mr_stock_valuation("002472")
        for clock in (datetime(2026, 9, 18, 12, 0), datetime(2026, 9, 18, 15, 1),
                      datetime(2026, 9, 19, 10, 0), datetime(2026, 9, 21, 9, 0),
                      datetime(2026, 9, 21, 9, 20)):
            with self.subTest(clock=clock):
                self.clock = clock
                self.assertEqual(market._mr_stock_valuation("002472"), first)
        self.mr.stock_ssjy_more.assert_called_once()
        self.budget.acquire.assert_called_once()

    def test_holiday_uses_cache(self):
        first = market._mr_stock_valuation("002472")
        self.clock += timedelta(days=10)
        with mock.patch.object(market.market_hours, "is_trading_day", return_value=False):
            self.assertEqual(market._mr_stock_valuation("002472"), first)
        self.mr.stock_ssjy_more.assert_called_once()

    def test_off_session_without_cache_fetches_once_and_survives_restart(self):
        for clock in (datetime(2026, 9, 18, 9, 20), datetime(2026, 9, 18, 12, 0),
                      datetime(2026, 9, 18, 15, 13), datetime(2026, 9, 19, 10, 0)):
            with self.subTest(clock=clock):
                self.clock = clock
                market._mr_valuation_cache.clear()
                (self.root / "000975.json").unlink(missing_ok=True)
                self.mr.stock_ssjy_more.reset_mock()
                self.budget.acquire.reset_mock()
                self.values = {"pe": 13.65, "pb_ratio": 4.14}
                expected = _snapshot(13.65, 4.14, clock.date().isoformat())
                self.assertEqual(market._mr_stock_valuation("000975"), expected)
                self.clock = clock.replace(hour=16, minute=0)
                self.assertEqual(market._mr_stock_valuation("000975"), expected)
                market._mr_valuation_cache.clear()
                self.assertEqual(market._mr_stock_valuation("000975"), expected)
                self.mr.stock_ssjy_more.assert_called_once_with(["000975"])
                self.budget.acquire.assert_called_once_with(timeout=3.5)
                disk = json.loads((self.root / "000975.json").read_text(encoding="utf-8"))
                self.assertEqual(disk["data"], expected)
                self.assertFalse(disk["session_refresh"])

    def test_preopen_initial_fetch_does_not_delay_first_session_refresh(self):
        self.clock = datetime(2026, 9, 18, 9, 20)
        market._mr_stock_valuation("000975")
        market._mr_valuation_cache.clear()
        self.clock = datetime(2026, 9, 18, 9, 30)
        self.values = {"pe": 13.65, "pb_ratio": 4.14}
        self.assertEqual(market._mr_stock_valuation("000975"), _snapshot(13.65, 4.14))
        self.assertEqual(self.mr.stock_ssjy_more.call_count, 2)
        self.clock += timedelta(minutes=29)
        market._mr_stock_valuation("000975")
        self.assertEqual(self.mr.stock_ssjy_more.call_count, 2)
        disk = json.loads((self.root / "000975.json").read_text(encoding="utf-8"))
        self.assertTrue(disk["session_refresh"])

    def test_off_session_failed_initial_fetch_backs_off(self):
        self.clock = datetime(2026, 9, 18, 15, 13)
        self.mr.stock_ssjy_more.return_value = []
        self.mr.stock_ssjy_more.side_effect = None
        self.assertIsNone(market._mr_stock_valuation("000975"))
        self.clock += timedelta(minutes=4)
        self.assertIsNone(market._mr_stock_valuation("000975"))
        self.mr.stock_ssjy_more.assert_called_once()
        self.clock += timedelta(minutes=1)
        self.mr.stock_ssjy_more.return_value = [{"dm": "000975", "pe": 13.65, "pb_ratio": 4.14}]
        self.assertEqual(market._mr_stock_valuation("000975"), _snapshot(13.65, 4.14))
        self.assertEqual(self.mr.stock_ssjy_more.call_count, 2)

    def test_off_session_core_without_cache_does_not_fetch(self):
        self.clock = datetime(2026, 9, 18, 15, 13)
        self.assertIsNone(market._mr_stock_valuation("000975", refresh=False))
        self.mr.stock_ssjy_more.assert_not_called()
        self.budget.acquire.assert_not_called()

    def test_disk_cache_survives_restart_and_weekend(self):
        first = market._mr_stock_valuation("002472")
        disk = json.loads((self.root / "002472.json").read_text(encoding="utf-8"))
        self.assertEqual(disk["day"], "2026-09-18")
        self.assertEqual(disk["data"], first)
        market._mr_valuation_cache.clear()
        self.clock = datetime(2026, 9, 19, 10, 0)
        self.assertEqual(market._mr_stock_valuation("002472"), first)
        self.mr.stock_ssjy_more.assert_called_once()

    def test_next_trading_day_requeries_and_persists(self):
        market._mr_stock_valuation("002472")
        self.clock = datetime(2026, 9, 21, 9, 30)
        self.values = {"pe": 26.0, "pb_ratio": 3.0}
        self.assertEqual(market._mr_stock_valuation("002472"), _snapshot(26.0, 3.0, "2026-09-21"))
        self.assertEqual(self.mr.stock_ssjy_more.call_count, 2)
        disk = json.loads((self.root / "002472.json").read_text(encoding="utf-8"))
        self.assertEqual(disk["day"], "2026-09-21")

    def test_only_requested_stock_is_refreshed(self):
        market._mr_stock_valuation("002472")
        old = (self.root / "002472.json").read_text(encoding="utf-8")
        self.clock += timedelta(minutes=31)
        market._mr_stock_valuation("000001")
        self.assertEqual(self.mr.stock_ssjy_more.call_args_list,
                         [mock.call(["002472"]), mock.call(["000001"])])
        self.assertEqual((self.root / "002472.json").read_text(encoding="utf-8"), old)

    def test_failed_refresh_preserves_last_success_and_retries_later(self):
        first = market._mr_stock_valuation("002472")
        old = (self.root / "002472.json").read_text(encoding="utf-8")
        self.clock += timedelta(minutes=30)
        self.mr.stock_ssjy_more.side_effect = RuntimeError("unavailable")
        self.assertEqual(market._mr_stock_valuation("002472"), first)
        self.clock += timedelta(minutes=4)
        self.assertEqual(market._mr_stock_valuation("002472"), first)
        self.assertEqual(self.mr.stock_ssjy_more.call_count, 2)
        self.assertEqual((self.root / "002472.json").read_text(encoding="utf-8"), old)
        self.clock += timedelta(minutes=1)
        self.mr.stock_ssjy_more.side_effect = None
        self.mr.stock_ssjy_more.return_value = [{"dm": "002472", "pe": 26.0, "pb_ratio": 3.0}]
        self.assertEqual(market._mr_stock_valuation("002472"), _snapshot(26.0, 3.0))
        self.assertEqual(self.mr.stock_ssjy_more.call_count, 3)

    def test_budget_wait_crossing_lunch_does_not_fetch(self):
        first = market._mr_stock_valuation("002472")
        self.clock += timedelta(minutes=30)
        self.mr.stock_ssjy_more.reset_mock()
        def admitted_after_lunch():
            self.clock = datetime(2026, 9, 18, 12, 0)
            return True
        self.budget.acquire.side_effect = lambda **kwargs: admitted_after_lunch()
        self.assertEqual(market._mr_stock_valuation("002472"), first)
        self.mr.stock_ssjy_more.assert_not_called()

    def test_core_cache_read_does_not_refresh_expired_value(self):
        first = market._mr_stock_valuation("002472")
        self.clock += timedelta(minutes=31)
        self.assertEqual(market._mr_stock_valuation("002472", refresh=False), first)
        self.mr.stock_ssjy_more.assert_called_once()

    def test_corrupt_disk_cache_can_be_repaired_outside_session(self):
        (self.root / "002472.json").write_text("broken", encoding="utf-8")
        self.clock = datetime(2026, 9, 19, 10, 0)
        self.assertEqual(market._mr_stock_valuation("002472"), _snapshot(25.33, 2.89, "2026-09-19"))
        self.mr.stock_ssjy_more.assert_called_once_with(["002472"])
        disk = json.loads((self.root / "002472.json").read_text(encoding="utf-8"))
        self.assertEqual(disk["data"], _snapshot(25.33, 2.89, "2026-09-19"))

    def test_nonfinite_data_does_not_replace_cache(self):
        first = market._mr_stock_valuation("002472")
        self.clock += timedelta(minutes=30)
        self.values = {"pe": "NaN", "pb_ratio": "Infinity"}
        self.assertEqual(market._mr_stock_valuation("002472"), first)

    def test_partial_refresh_retains_other_field_on_disk_and_after_restart(self):
        for code, values, expected in (
            ("002472", {"pe": 26.0, "pb_ratio": "--"}, _snapshot(26.0, 2.89)),
            ("000001", {"pe": "NaN", "pb_ratio": 0.0}, _snapshot(25.33, 0.0)),
        ):
            with self.subTest(code=code):
                self.clock = datetime(2026, 9, 18, 10, 0)
                self.values = {"pe": 25.33, "pb_ratio": 2.89}
                market._mr_stock_valuation(code)
                self.clock += timedelta(minutes=30)
                self.values = values
                self.assertEqual(market._mr_stock_valuation(code), expected)
                disk = json.loads((self.root / f"{code}.json").read_text(encoding="utf-8"))
                self.assertEqual(disk["data"], expected)
                market._mr_valuation_cache.clear()
                self.clock = datetime(2026, 9, 19, 10, 0)
                self.assertEqual(market._mr_stock_valuation(code), expected)

    def test_core_quote_token_does_not_starve_valuation_refresh(self):
        budget = market.PacedBudget(20)
        ticks = [100.0]

        def sleep(seconds):
            ticks[0] += seconds

        def quote(symbol):
            self.assertTrue(budget.try_acquire())
            return {"name": "测试", "last_price": 35, "prev_close": 34, "volume": 100}

        with mock.patch.object(market, "_mr_quote_budget", budget), \
             mock.patch.object(market.time, "monotonic", side_effect=lambda: ticks[0]), \
             mock.patch.object(market.time, "sleep", side_effect=sleep), \
             mock.patch.object(market, "_stock_info_cache", {}), \
             mock.patch.object(market, "_stock_info_enrichment_cache", {}), \
             mock.patch.object(market, "_is_etf", return_value=False), \
             mock.patch.object(market, "_is_index_symbol", return_value=False), \
             mock.patch.object(market, "_fetch_instrument_meta", return_value=None), \
             mock.patch.object(market, "_mr_industry", return_value=None), \
             mock.patch.object(market, "_mr_instrument", return_value=None), \
             mock.patch.object(market, "fetch_kline_ex", return_value=(None, None, None)), \
             mock.patch.object(market, "fetch_quote", side_effect=quote):
            info = market.fetch_stock_info("002472.SZ")
        self.assertEqual((info["pe"], info["pb"]), (25.33, 2.89))
        self.mr.stock_ssjy_more.assert_called_once_with(["002472"])
        self.assertEqual(budget.limit, 20)
        self.assertEqual(list(budget.starts), [100.0, 103.0])

    def test_session_gate_observes_seconds_and_microseconds(self):
        for i, (clock, allowed) in enumerate((
            (datetime(2026, 9, 18, 9, 29, 59), False),
            (datetime(2026, 9, 18, 9, 30), True),
            (datetime(2026, 9, 18, 11, 30), True),
            (datetime(2026, 9, 18, 11, 30, 0, 1), False),
            (datetime(2026, 9, 18, 11, 30, 59), False),
            (datetime(2026, 9, 18, 12, 59, 59), False),
            (datetime(2026, 9, 18, 13, 0), True),
            (datetime(2026, 9, 18, 15, 0), True),
            (datetime(2026, 9, 18, 15, 0, 0, 1), False),
            (datetime(2026, 9, 18, 15, 0, 59), False),
        )):
            with self.subTest(clock=clock):
                self.clock = clock
                self.mr.stock_ssjy_more.reset_mock()
                self.budget.acquire.reset_mock()
                code = f"{600000 + i:06}"
                market._mr_valuation_cache[code] = {
                    "ts": self._epoch() - 1800, "day": clock.date().isoformat(),
                    "data": {"pe": 25.33, "pb": 2.89},
                }
                market._mr_stock_valuation(code)
                self.assertEqual(self.mr.stock_ssjy_more.call_count, int(allowed))
                self.assertEqual(self.budget.acquire.call_count, int(allowed))

    def test_budget_wait_crossing_exact_cutoff_does_not_fetch(self):
        for cutoff in (datetime(2026, 9, 18, 11, 30, 59), datetime(2026, 9, 18, 15, 0, 59)):
            with self.subTest(cutoff=cutoff):
                self.clock = datetime(2026, 9, 18, 10, 0)
                self.mr.stock_ssjy_more.reset_mock()
                first = {"pe": 25.33, "pb": 2.89}
                market._mr_valuation_cache["002472"] = {
                    "ts": self._epoch() - 1800, "day": "2026-09-18", "data": first,
                }

                def admitted():
                    self.clock = cutoff
                    return True

                self.budget.acquire.side_effect = lambda **kwargs: admitted()
                self.assertEqual(market._mr_stock_valuation("002472"), first)
                self.mr.stock_ssjy_more.assert_not_called()

    def test_concurrent_queries_for_same_stock_share_refresh(self):
        # 盘外首次补取也必须由同股锁合并, 不能因多个信息栏请求重复回源。
        self.clock = datetime(2026, 9, 18, 15, 13)
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []

        def fetch(codes):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test timed out")
            return [{"dm": codes[0], **self.values}]

        def query():
            try:
                results.append(market._mr_stock_valuation("002472"))
            except Exception as error:
                errors.append(error)

        self.mr.stock_ssjy_more.side_effect = fetch
        threads = [threading.Thread(target=query) for _ in range(3)]
        try:
            for thread in threads:
                thread.start()
            self.assertTrue(entered.wait(1))
        finally:
            release.set()
            for thread in threads:
                thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(results, [_snapshot(25.33, 2.89)] * 3)
        self.mr.stock_ssjy_more.assert_called_once()

    def _enable_rankings(self):
        self.values = {}
        self.mr.hsdc_himk_syl.return_value = [
            {"dm": "sz002472", "t": "2026-09-17", "jpe": 999, "dpe": 24.067},
            {"dm": "SZ000975", "t": "2026-09-17", "jpe": 999, "dpe": 17.053},
            {"dm": "600000.SH", "t": "2026-09-17", "dpe": 0},
        ]
        self.mr.hsdc_himk_sjl.return_value = [
            {"dm": "002472.SZ", "t": "2026-09-17", "sjl": 2.945},
            {"dm": "sz000975", "t": "2026-09-17", "sjl": 4.058},
            {"dm": "sh600000", "t": "2026-09-17", "sjl": -1},
        ]

    def test_rankings_cover_target_stocks_prefixes_zero_and_negative(self):
        self._enable_rankings()
        for code, expected in (("002472", (24.067, 2.945)), ("000975", (17.053, 4.058)),
                               ("600000", (0.0, -1.0))):
            with self.subTest(code=code):
                data = market._mr_stock_valuation(code)
                self.assertEqual((data["pe"], data["pb"]), expected)
                self.assertEqual((data["pe_source"], data["pb_source"]), ("pe_ranking", "pb_ranking"))
                self.assertEqual((data["pe_date"], data["pb_date"]), ("2026-09-17", "2026-09-17"))
        self.mr.hsdc_himk_syl.assert_called_once_with()
        self.mr.hsdc_himk_sjl.assert_called_once_with()
        self.assertEqual(self.mr.stock_ssjy_more.call_count, 3)
        self.assertEqual(self.budget.acquire.call_count, 5)
        self.assertEqual(self.budget.interval, 3.0)
        self.assertEqual(set(p.stem for p in self.root.glob("*.json")), {"002472", "000975", "600000"})

    def test_only_missing_field_fetches_corresponding_ranking(self):
        for missing in ("pe", "pb"):
            with self.subTest(missing=missing):
                market._mr_valuation_cache.clear()
                (self.root / "000975.json").unlink(missing_ok=True)
                self._enable_rankings()
                self.mr.hsdc_himk_syl.reset_mock()
                self.mr.hsdc_himk_sjl.reset_mock()
                self.values = {"pb_ratio": 4.14} if missing == "pe" else {"pe": 13.65}
                data = market._mr_stock_valuation("000975")
                if missing == "pe":
                    self.assertEqual((data["pe"], data["pb"]), (17.053, 4.14))
                    self.assertEqual(data["pb_source"], "snapshot")
                    self.mr.hsdc_himk_syl.assert_called_once()
                    self.mr.hsdc_himk_sjl.assert_not_called()
                else:
                    self.assertEqual((data["pe"], data["pb"]), (13.65, 4.058))
                    self.assertEqual(data["pe_source"], "snapshot")
                    self.mr.hsdc_himk_syl.assert_not_called()
                    self.mr.hsdc_himk_sjl.assert_called_once()
                self.assertEqual(data[f"{missing}_date"], "2026-09-17")

    def test_previous_snapshot_has_priority_over_rankings(self):
        first = market._mr_stock_valuation("000975")
        self.clock += timedelta(minutes=30)
        self._enable_rankings()
        self.assertEqual(market._mr_stock_valuation("000975"), first)
        self.mr.hsdc_himk_syl.assert_not_called()
        self.mr.hsdc_himk_sjl.assert_not_called()

    def test_retained_snapshot_keeps_its_own_date_when_other_field_refreshes(self):
        market._mr_stock_valuation("000975")
        self.clock = datetime(2026, 9, 21, 10, 0)
        self.values = {"pe": None, "pb_ratio": 4.14, "t": "2026-09-21 10:00:00"}
        data = market._mr_stock_valuation("000975")
        self.assertEqual((data["pe"], data["pb"]), (25.33, 4.14))
        self.assertEqual((data["pe_date"], data["pb_date"]), ("2026-09-18", "2026-09-21"))
        self.mr.hsdc_himk_syl.assert_not_called()

    def test_ranking_budget_denial_preserves_shared_cache_without_request(self):
        self._enable_rankings()
        first = market._mr_valuation_rank("pe")
        self.clock += timedelta(hours=12)
        self.budget.acquire.return_value = False
        self.assertEqual(market._mr_valuation_rank("pe", allow_outside=True), first)
        self.mr.hsdc_himk_syl.assert_called_once()

    def test_snapshot_budget_denial_still_fills_from_shared_rank_cache(self):
        self._enable_rankings()
        market._mr_valuation_rank("pe")
        market._mr_valuation_rank("pb")
        self.budget.acquire.reset_mock()
        self.budget.acquire.return_value = False
        for restart, code in ((False, "000975"), (True, "002472")):
            with self.subTest(restart=restart):
                if restart:
                    market._mr_valuation_rank_cache.clear()
                data = market._mr_stock_valuation(code)
                self.assertIsNotNone(data)
                self.assertEqual((data["pe_source"], data["pb_source"]), ("pe_ranking", "pb_ranking"))
                self.assertEqual((data["pe_date"], data["pb_date"]), ("2026-09-17", "2026-09-17"))
        self.mr.stock_ssjy_more.assert_not_called()
        self.mr.hsdc_himk_syl.assert_called_once()
        self.mr.hsdc_himk_sjl.assert_called_once()
        self.assertEqual(self.budget.acquire.call_count, 2, "预算拒绝后只能读日榜缓存, 不能再申请令牌")

    def test_budget_denied_cache_read_does_not_wait_for_ranking_network_locks(self):
        self._enable_rankings()
        market._mr_valuation_rank("pe")
        market._mr_valuation_rank("pb")
        market._mr_valuation_rank_cache.clear()
        self.budget.acquire.return_value = False
        done, results, errors = threading.Event(), [], []

        def query():
            try:
                results.append(market._mr_stock_valuation("000975"))
            except Exception as error:
                errors.append(error)
            finally:
                done.set()

        locks = [market._mr_valuation_rank_locks[field] for field in ("pe", "pb")]
        for lock in locks:
            lock.acquire()
        thread = threading.Thread(target=query)
        try:
            thread.start()
            completed_without_release = done.wait(1)
        finally:
            for lock in reversed(locks):
                lock.release()
            thread.join(3)
        self.assertTrue(completed_without_release, "纯日榜缓存读取不能等待榜单网络锁")
        self.assertEqual(errors, [])
        self.assertEqual((results[0]["pe"], results[0]["pb"]), (17.053, 4.058))
        self.mr.stock_ssjy_more.assert_not_called()
        self.mr.hsdc_himk_syl.assert_called_once()
        self.mr.hsdc_himk_sjl.assert_called_once()

    def test_partial_rank_refresh_keeps_omitted_and_invalid_rows_on_disk(self):
        self._enable_rankings()
        for field, method, key in (("pe", "hsdc_himk_syl", "dpe"), ("pb", "hsdc_himk_sjl", "sjl")):
            with self.subTest(field=field):
                first = market._mr_valuation_rank(field, allow_outside=True)
                self.clock += timedelta(hours=12)
                getattr(self.mr, method).return_value = [
                    {"dm": "sz002472", "t": "2026-09-18", key: 0},
                    {"dm": "sz000975", "t": "2026-09-18", key: "NaN"},
                ]
                refreshed = market._mr_valuation_rank(field, allow_outside=True)
                self.assertEqual(refreshed.get("000975"), first["000975"])
                self.assertEqual(refreshed.get("600000"), first["600000"])
                self.assertEqual(refreshed["002472"], {"value": 0.0, "date": "2026-09-18"})
                disk = json.loads((self.root / "rankings" / f"{field}.json").read_text(encoding="utf-8"))
                self.assertEqual(disk["data"], refreshed)
                market._mr_valuation_rank_cache.clear()
                self.assertEqual(market._mr_valuation_rank(field, allow_outside=True), refreshed)

    def test_stock_with_no_individual_cache_uses_retained_partial_rank_record(self):
        self._enable_rankings()
        market._mr_valuation_rank("pe")
        market._mr_valuation_rank("pb")
        self.clock = datetime(2026, 9, 21, 10, 0)
        self.mr.hsdc_himk_syl.return_value = [{"dm": "sz002472", "t": "2026-09-18", "dpe": 25}]
        self.mr.hsdc_himk_sjl.return_value = [{"dm": "sz002472", "t": "2026-09-18", "sjl": 3}]
        market._mr_valuation_rank("pe")
        market._mr_valuation_rank("pb")
        market._mr_valuation_rank_cache.clear()
        data = market._mr_stock_valuation("000975")
        self.assertEqual((data["pe"], data["pb"]), (17.053, 4.058))
        self.assertEqual((data["pe_date"], data["pb_date"]), ("2026-09-17", "2026-09-17"))

    def test_core_cold_read_does_not_wait_for_shared_network_lock(self):
        code = "000975"
        path = self.root / f"{code}.json"
        lock = market._mr_valuation_locks[hash(code) % len(market._mr_valuation_locks)]
        for disk_present in (False, True):
            with self.subTest(disk_present=disk_present):
                market._mr_valuation_cache.clear()
                if disk_present:
                    path.write_text(json.dumps({"code": code, "ts": self._epoch(), "day": "2026-09-18",
                                               "data": {"pe": 13.65, "pb": 4.14}}), encoding="utf-8")
                done, results, errors = threading.Event(), [], []

                def read():
                    try:
                        results.append(market._mr_stock_valuation(code, refresh=False))
                    except Exception as error:
                        errors.append(error)
                    finally:
                        done.set()

                lock.acquire()
                thread = threading.Thread(target=read)
                try:
                    thread.start()
                    completed_without_release = done.wait(1)
                finally:
                    lock.release()
                    thread.join(3)
                self.assertTrue(completed_without_release, "核心首包不能等待估值网络锁释放")
                self.assertEqual(errors, [])
                self.assertEqual(results, [_snapshot(13.65, 4.14) if disk_present else None])
        self.mr.stock_ssjy_more.assert_not_called()
        self.mr.hsdc_himk_syl.assert_not_called()
        self.mr.hsdc_himk_sjl.assert_not_called()

    def test_ranked_value_recovers_to_snapshot_at_next_refresh(self):
        self._enable_rankings()
        first = market._mr_stock_valuation("000975")
        self.assertEqual(first["pe_source"], "pe_ranking")
        market._mr_valuation_cache.clear()
        self.clock += timedelta(minutes=29)
        self.assertEqual(market._mr_stock_valuation("000975"), first)
        self.values = {"pe": 13.65, "pb_ratio": 4.14, "t": "2026-09-18 10:30:00"}
        self.clock += timedelta(minutes=1)
        recovered = market._mr_stock_valuation("000975")
        self.assertEqual(recovered, _snapshot(13.65, 4.14))
        self.assertEqual(self.mr.stock_ssjy_more.call_count, 2)
        self.mr.hsdc_himk_syl.assert_called_once()
        self.mr.hsdc_himk_sjl.assert_called_once()
        disk = json.loads((self.root / "000975.json").read_text(encoding="utf-8"))
        self.assertEqual(disk["data"], recovered)

    def test_ranking_cache_survives_restart_for_other_queried_stock(self):
        self._enable_rankings()
        market._mr_stock_valuation("000975")
        market._mr_valuation_cache.clear()
        market._mr_valuation_rank_cache.clear()
        data = market._mr_stock_valuation("002472")
        self.assertEqual((data["pe"], data["pb"]), (24.067, 2.945))
        self.mr.hsdc_himk_syl.assert_called_once()
        self.mr.hsdc_himk_sjl.assert_called_once()
        self.assertEqual(self.budget.acquire.call_count, 4)

    def test_off_session_initial_ranking_reuses_stock_cache_after_restart(self):
        self._enable_rankings()
        self.clock = datetime(2026, 9, 18, 16, 0)
        first = market._mr_stock_valuation("000975")
        self.clock += timedelta(days=1)
        market._mr_valuation_cache.clear()
        market._mr_valuation_rank_cache.clear()
        self.assertEqual(market._mr_stock_valuation("000975"), first)
        self.mr.stock_ssjy_more.assert_called_once()
        self.mr.hsdc_himk_syl.assert_called_once()
        self.mr.hsdc_himk_sjl.assert_called_once()

    def test_next_day_missing_snapshot_refreshes_expired_rankings(self):
        self._enable_rankings()
        market._mr_stock_valuation("000975")
        self.clock = datetime(2026, 9, 21, 9, 30)
        self.mr.hsdc_himk_syl.return_value = [{"dm": "sz000975", "t": "2026-09-18", "dpe": 18}]
        self.mr.hsdc_himk_sjl.return_value = [{"dm": "sz000975", "t": "2026-09-18", "sjl": 4.5}]
        data = market._mr_stock_valuation("000975")
        self.assertEqual((data["pe"], data["pb"]), (18.0, 4.5))
        self.assertEqual((data["pe_date"], data["pb_date"]), ("2026-09-18", "2026-09-18"))
        self.assertEqual(self.mr.hsdc_himk_syl.call_count, 2)
        self.assertEqual(self.mr.hsdc_himk_sjl.call_count, 2)

    def test_global_ranking_ttl_and_failure_backoff_preserve_disk(self):
        self._enable_rankings()
        first = market._mr_valuation_rank("pe")
        path = self.root / "rankings" / "pe.json"
        disk = path.read_bytes()
        self.clock += timedelta(hours=11)
        self.assertEqual(market._mr_valuation_rank("pe", allow_outside=True), first)
        self.mr.hsdc_himk_syl.assert_called_once()
        self.clock += timedelta(hours=1)
        self.mr.hsdc_himk_syl.side_effect = RuntimeError("unavailable")
        self.assertEqual(market._mr_valuation_rank("pe", allow_outside=True), first)
        self.assertEqual(path.read_bytes(), disk)
        self.clock += timedelta(minutes=4)
        self.assertEqual(market._mr_valuation_rank("pe", allow_outside=True), first)
        self.assertEqual(self.mr.hsdc_himk_syl.call_count, 2)
        self.clock += timedelta(minutes=1)
        self.mr.hsdc_himk_syl.side_effect = None
        self.mr.hsdc_himk_syl.return_value = [{"dm": "sz000975", "t": "2026-09-18", "dpe": 18}]
        self.assertEqual(market._mr_valuation_rank("pe", allow_outside=True)["000975"]["value"], 18)
        self.assertEqual(self.mr.hsdc_himk_syl.call_count, 3)

    def test_empty_rankings_and_snapshot_back_off_without_writing_empty_cache(self):
        self.values = {}
        self.assertIsNone(market._mr_stock_valuation("000975"))
        self.clock += timedelta(minutes=4)
        self.assertIsNone(market._mr_stock_valuation("000975"))
        self.mr.stock_ssjy_more.assert_called_once()
        self.mr.hsdc_himk_syl.assert_called_once()
        self.mr.hsdc_himk_sjl.assert_called_once()
        self.assertEqual(list(self.root.rglob("*.json")), [])
        self.clock += timedelta(minutes=1)
        self._enable_rankings()
        self.assertEqual(market._mr_stock_valuation("000975")["pe"], 17.053)
        self.assertEqual(self.mr.hsdc_himk_syl.call_count, 2)

    def test_invalid_ranking_rows_are_ignored_without_using_static_pe(self):
        self._enable_rankings()
        self.mr.hsdc_himk_syl.return_value = [None, "bad", {"dm": "sz000975", "jpe": 10},
            {"dm": "sz000975", "t": "2026-09-17", "dpe": "NaN"},
            {"dm": "sz000975", "t": "bad", "dpe": 10},
            {"dm": "oops", "t": "2026-09-17", "dpe": 10}]
        data = market._mr_stock_valuation("000975")
        self.assertIsNone(data["pe"])
        self.assertNotIn("pe_source", data)
        self.assertEqual((data["pb"], data["pb_source"]), (4.058, "pb_ranking"))

    def test_missing_current_rank_row_keeps_previous_stock_rank_value(self):
        self._enable_rankings()
        first = market._mr_stock_valuation("000975")
        self.clock = datetime(2026, 9, 21, 10, 0)
        self.mr.hsdc_himk_syl.return_value = [{"dm": "sz002472", "t": "2026-09-18", "dpe": 25}]
        self.mr.hsdc_himk_sjl.return_value = []
        self.assertEqual(market._mr_stock_valuation("000975"), first)

    def test_legacy_stock_cache_defaults_to_snapshot_source(self):
        raw = {"code": "000975", "ts": self._epoch(), "day": "2026-09-18",
               "data": {"pe": 13.65, "pb": 4.14}}
        (self.root / "000975.json").write_text(json.dumps(raw), encoding="utf-8")
        self.assertEqual(market._mr_stock_valuation("000975", refresh=False), _snapshot(13.65, 4.14))
        self.mr.stock_ssjy_more.assert_not_called()
        self.mr.hsdc_himk_syl.assert_not_called()
        self.mr.hsdc_himk_sjl.assert_not_called()

    def test_budget_wait_crossing_close_skips_ranking_refresh(self):
        self._enable_rankings()
        self.values = {"pe": 13.65}
        market._mr_valuation_cache["000975"] = {
            "ts": self._epoch() - 1800, "day": "2026-09-18", "data": {"pe": 13.6, "pb": None}}

        def acquire(**kwargs):
            if self.budget.acquire.call_count == 2:
                self.clock = datetime(2026, 9, 18, 15, 0, 0, 1)
            return True

        self.budget.acquire.side_effect = acquire
        data = market._mr_stock_valuation("000975")
        self.assertEqual(data["pe"], 13.65)
        self.assertIsNone(data["pb"])
        self.mr.hsdc_himk_syl.assert_not_called()
        self.mr.hsdc_himk_sjl.assert_not_called()

    def test_two_concurrent_stocks_share_one_request_per_ranking(self):
        self._enable_rankings()
        self.clock = datetime(2026, 9, 18, 16, 0)
        entered, release = threading.Event(), threading.Event()
        rows = self.mr.hsdc_himk_syl.return_value
        results, errors = {}, []

        def fetch():
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test timed out")
            return rows

        def query(code):
            try:
                results[code] = market._mr_stock_valuation(code)
            except Exception as error:
                errors.append(error)

        self.mr.hsdc_himk_syl.side_effect = fetch
        threads = [threading.Thread(target=query, args=(code,)) for code in ("000975", "002472")]
        try:
            for thread in threads:
                thread.start()
            self.assertTrue(entered.wait(1))
        finally:
            release.set()
            for thread in threads:
                thread.join(3)
        self.assertEqual(errors, [])
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(set(results), {"000975", "002472"})
        self.mr.hsdc_himk_syl.assert_called_once()
        self.mr.hsdc_himk_sjl.assert_called_once()


if __name__ == "__main__":
    unittest.main()
