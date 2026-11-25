"""Verify compact chart responses and off-session snapshot behavior without network calls."""
import gzip
import json
import struct
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import market
import quote_snapshots
import trades
from api import kline, market as market_api


class ChartTransportTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def test_intraday_delta_revises_older_minutes_and_resets_across_days(self):
        def get(day, bars, since=""):
            with self.app.test_request_context(f"/?since={since}"):
                return kline._intraday_response("000792.SZ", "1m", 240, day, bars, is_index=False).get_json()

        base = get("2026-11-23", [{"time": "09:31", "close": 10}, {"time": "09:32", "close": 11}])
        fixed = [{"time": "09:31", "close": 11}, {"time": "09:32", "close": 11}]
        result = get("2026-11-23", fixed, base["version"])
        self.assertTrue(result["delta"])
        self.assertEqual(result["rows"], [["09:31", 11]])
        self.assertEqual(get("2026-11-23", fixed, result["version"])["rows"], [])
        self.assertFalse(get("2026-11-23", fixed[1:], result["version"])["delta"])
        self.assertFalse(get("2026-11-24", fixed, result["version"])["delta"])

    def test_chart_rows_keep_precision_and_history_window(self):
        frame = pd.DataFrame({"open": 8., "high": 11., "low": 7., "close": 9.,
                              "volume": 10000, "amount": 90000.},
                             index=pd.date_range("2022-01-01", periods=1006))

        def request(query):
            with self.app.test_request_context("/?symbol=000792.SZ&count=1006" + query):
                return kline.kline()

        with patch.object(kline, "fetch_kline_ex", return_value=(frame, "stock", "test")), \
             patch.object(kline, "_attach_quote"), patch.object(kline, "peek_instrument_meta", return_value=None), \
             patch.object(kline, "_is_index_symbol", return_value=False), \
             patch.object(kline.market_hours, "is_trading_day", return_value=True):
            old = request("")
            packed = request("&format=rows&limit=300&warmup=249")
            data = packed.get_json()
            older = request("&format=rows&warmup=249&before=" + data["rows"][0][0]).get_json()
        self.assertTrue(data["has_more"])
        self.assertFalse(older["has_more"])
        self.assertEqual(len(data["rows"]), 300)
        self.assertEqual(data["warmup_closes"], [9.] * 249)
        self.assertEqual(older["warmup_closes"], [])
        rows = [dict(zip(data["fields"], r)) for r in older["rows"] + data["rows"]]
        legacy = old.get_json()["klines"]
        self.assertEqual(len(rows), len(legacy))
        for bar, original in zip(rows, legacy):
            for field, value in original.items():
                if field in kline._CHART_INDICATORS and value is not None:
                    self.assertLessEqual(abs(value - bar[field]), 0.00000051, field)
                else:
                    self.assertEqual(bar[field], value, field)
        self.assertLess(len(gzip.compress(packed.data)), len(gzip.compress(old.data)) * .35)

    def test_long_chart_history_crosses_local_boundary_and_replaces_partial_period(self):
        daily = pd.DataFrame({"open": 8., "high": 11., "low": 7., "close": 9.,
                              "volume": 10000, "amount": 90000.},
                             index=pd.bdate_range("2019-01-01", periods=930))
        from daily_chart_store import resample_bars

        def get(period, query="", history="1"):
            with self.app.test_request_context(
                    f"/?symbol=300413.SZ&period={period}&format=rows&history={history}&warmup=249{query}"):
                return kline.kline()

        for period in ("1d", "1w", "1M"):
            with self.subTest(period=period):
                full = daily if period == "1d" else resample_bars(daily, period)
                local = daily.iloc[707:]
                if period != "1d":
                    local = resample_bars(local, period)
                cursor = local.index[0]

                def provider(symbol, period, count, adjust, end_time):
                    day = pd.Timestamp(end_time, unit="ms", tz="Asia/Shanghai").date()
                    return full.loc[:str(day)].tail(count)

                with patch.object(kline, "fetch_kline_ex", return_value=(local, "stock", "daily_chart")), \
                     patch.object(kline, "_attach_quote"), patch.object(kline, "peek_instrument_meta", return_value=None), \
                     patch.object(kline, "_is_index_symbol", return_value=False), \
                     patch.object(kline, "fetch_af_history", side_effect=provider) as upstream:
                    first = get(period, "&limit=300").get_json()
                    self.assertTrue(first["has_more"])
                    self.assertTrue(first["history_remote"])
                    upstream.assert_not_called()
                    query = f"&before={cursor.date()}&limit=1000&anchor_close=9"
                    page = get(period, query, history="remote").get_json()
                    self.assertEqual(page["rows"][-1][0], full.loc[:cursor].index[-2].date().isoformat())
                    self.assertLessEqual(len(page["rows"]), 300)
                    if period != "1d":
                        anchor = dict(zip(page["fields"], page["anchor_row"]))
                        self.assertEqual(anchor["date"], cursor.date().isoformat())
                        self.assertEqual(anchor["volume"], full.loc[cursor, "volume"])
                        self.assertGreater(anchor["volume"], local.loc[cursor, "volume"])
                    self.assertTrue(page["has_more"])
                    end = get(period, f"&before={full.index[0].date()}&anchor_close=9").get_json()
                    self.assertFalse(end["has_more"])
                    self.assertEqual(end["history_end"], full.index[0].date().isoformat())
                    self.assertEqual(len(end["rows"]), 0)

    def test_history_failed_requests_can_retry_and_success_is_cached(self):
        frame = pd.DataFrame({"open": 8., "high": 11., "low": 7., "close": 9.,
                              "volume": 10000, "amount": 90000.},
                             index=pd.bdate_range("2021-01-01", periods=3))
        with patch.object(kline, "fetch_af_history", side_effect=[None, frame, frame]) as upstream, \
             patch.object(kline, "_listing_day", return_value=None), \
             self.app.test_request_context("/"):
            args = ("TEST.SZ", "1d", "forward", frame.index[-1].date())
            self.assertEqual(kline._history_response(*args, 9., 41, 0).status_code, 503)
            self.assertEqual(kline._history_response(*args, 5., 41, 0).status_code, 503)
            for _ in range(2):
                result = kline._history_response(*args, 9., 41, 0)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(len(result.get_json()["rows"]), 2)
            self.assertEqual(upstream.call_count, 3)

    def test_local_history_cache_validates_each_anchor_close(self):
        frame = pd.DataFrame({"open": 8., "high": 11., "low": 7., "close": 9.,
                              "volume": 10000, "amount": 90000.},
                             index=pd.date_range("2024-01-05", periods=12, freq="W-FRI"))

        def get(close):
            with self.app.test_request_context(
                    f"/?symbol=300413.SZ&period=1w&format=rows&history=1"
                    f"&before={frame.index[7].date()}&limit=4&warmup=3&anchor_close={close}"):
                return kline.kline()

        with patch.object(kline, "kline_cache_long", market.TTLCache()), \
             patch.object(kline, "fetch_kline_ex", return_value=(frame, "stock", "daily_chart")) as source, \
             patch.object(kline, "_attach_quote"), patch.object(kline, "_is_etf", return_value=False), \
             patch.object(kline, "peek_instrument_meta", return_value=None):
            self.assertFalse(get(9).get_json()["meta"]["cached"])
            self.assertTrue(get(9).get_json()["meta"]["cached"])
            self.assertEqual(get(31).status_code, 503)
            self.assertEqual(source.call_count, 2)

    def test_history_indicator_post_recalculates_obv_and_ma_across_pages(self):
        rows = [[price, price, price, price, 100] for price in range(1, 1001)]
        with self.app.test_request_context(
                "/api/kline", method="POST", json={"period": "1d", "rows": rows, "warmup": 249}):
            response = kline.kline_history_indicators()
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        bars = [dict(zip(data["fields"], row)) for row in data["rows"]]
        self.assertEqual(len(bars), 751)
        self.assertEqual(bars[51]["ma20"], 291.5)
        self.assertEqual(bars[301]["obv"] - bars[300]["obv"], 100)
        self.assertEqual(bars[301]["maobv"] - bars[300]["maobv"], 100)
        self.assertNotIn("open", data["fields"])

    def test_chart_transport_shortens_values_and_preserves_crossings(self):
        bar = {"date": "2026-11-25", "open": 12.300000190734863, "close": 12.3000001,
               "volume": 100, "amount": 12.300000190734863, "impulse": -1,
               "rsi6": 52.123456789, "rsi12": None,
               "ma5": 12.30000049, "ma20": 12.30000041,
               "macd_dif": 0.01500041, "macd_dea": 0.01500049, "macd_hist": -0.00000016}
        sent = kline.compact_chart_tail({"bars": [bar]})["bars"][0]
        self.assertEqual(sent["open"], 12.3)
        self.assertEqual(struct.pack("<f", sent["open"]), struct.pack("<f", bar["open"]))
        self.assertEqual(sent["rsi6"], 52.123457)
        for field in ("close", "volume", "amount", "impulse", "rsi12"):
            self.assertEqual(sent[field], bar[field])
        self.assertGreater(sent["ma5"], sent["ma20"])
        self.assertLess(sent["macd_dif"], sent["macd_dea"])
        self.assertLess(sent["macd_hist"], 0)
        self.assertEqual(bar["open"], 12.300000190734863)
        self.assertEqual(bar["rsi6"], 52.123456789)

    def test_trade_chart_view_projects_batch_legs_and_preserves_user_scope(self):
        original_db = trades._db_path
        with tempfile.TemporaryDirectory() as path:
            try:
                trades.init_db(Path(path) / "trades.db")
                trades.create_user("chart_user", "pass12345")
                trades.create_user("another_user", "pass12345")
                ids = {user["username"]: user["id"] for user in trades.list_users()}
                with closing(trades.get_conn()) as conn, conn:
                    cursor = conn.execute(
                        "INSERT INTO trades(user_id,symbol,name,type,status,entry_date,entry_price,quantity,entry_reason,created_at,updated_at) "
                        "VALUES (?, '000792.SZ', 'stock', 'batch', 'open', '2026-11-23', 9, 100, '', 'now', 'now')",
                        (ids["chart_user"],),
                    )
                    trades._insert_legs(conn, cursor.lastrowid, [{"side": "buy", "price": 9,
                                                                 "quantity": 100, "date": "2026-11-23", "note": "private"}])
                filters = {"symbol": "000792.SZ"}
                with patch.object(trades, "_trade_metrics", side_effect=AssertionError("chart should skip fees")):
                    rows, total = trades.list_trades(ids["chart_user"], filters, chart=True)
                    self.assertEqual(trades.list_trades(ids["another_user"], filters, chart=True), ([], 0))
                self.assertEqual(total, 1)
                self.assertEqual(rows[0]["cost_price"], 9)
                self.assertEqual(rows[0]["legs"], [{"side": "buy", "price": 9, "quantity": 100, "date": "2026-11-23"}])
            finally:
                trades._db_path = original_db

    def test_chart_pledge_cache_recovers_without_blocking_core(self):
        pledge = {"000792": {"ratio": 7.5}}
        request = "/?symbol=000792.SZ&core=1&view=chart"
        with self.app.test_request_context(request), \
             patch.object(market_api, "fetch_stock_info", return_value={}), \
             patch.multiple(market, _pledge_cache={}, _pledge_ts=0), \
             patch.object(market, "_refresh_pledge_async") as refresh:
            self.assertNotIn("pledge_ratio", market_api.stock_info().get_json())
            refresh.assert_called_once_with(on_read=True)
            market._pledge_cache = pledge
            self.assertEqual(market_api.stock_info().get_json()["pledge_ratio"], 7.5)
            self.assertEqual(refresh.call_count, 2)

    def test_off_session_quote_recheck_reads_cache_only(self):
        symbol = "000792.SZ"
        with self.app.test_request_context(f"/?symbols={symbol}&cache_only=1"), \
             patch.object(quote_snapshots, "current", return_value=None), \
             patch.object(market, "_quote_cache_lookup", return_value={"last_price": 10}), \
             patch.object(market_api, "fetch_quotes") as fetch:
            self.assertEqual(market_api.quotes().get_json()[symbol]["last_price"], 10)
            fetch.assert_not_called()


class SnapshotWorkerTest(unittest.TestCase):
    def test_new_receipt_for_previous_day_still_needs_live_fallback(self):
        now = datetime(2026, 11, 25, 10)
        symbol = "000792.SZ"
        worker = quote_snapshots.SnapshotWorker(Mock())
        worker.pools["CN_Stock"][symbol] = {
            "quote_trade_date": "2026-11-24", "quote_status": "close_confirmed",
            "snapshot_received_at": now.replace(tzinfo=quote_snapshots.CST).timestamp(),
        }
        quote, refresh = worker.lookup(symbol, now)
        self.assertEqual(quote["quote_status"], "last_quote")
        self.assertTrue(refresh)

    def test_midday_recovers_only_pool_without_recent_morning_data(self):
        worker = quote_snapshots.SnapshotWorker(Mock())
        day = "2026-11-25"
        symbol = "000792.SZ"
        cst = quote_snapshots.CST
        worker.morning_latest["CN_Stock"] = datetime(2026, 11, 25, 11, tzinfo=cst)
        worker.morning_latest["CN_ETF"] = datetime(2026, 11, 25, 11, 30, tzinfo=cst)
        worker.pools["CN_Stock"][symbol] = {
            "quote_trade_date": day, "quote_status": "live",
            "timestamp": datetime(2026, 11, 25, 9, 20, tzinfo=cst).timestamp(),
            "snapshot_received_at": datetime(2026, 11, 25, 9, 20, tzinfo=cst).timestamp(),
        }
        with patch.object(worker, "_fetch", return_value=True) as fetch:
            worker._recover_midday(day)
            worker._recover_midday(day)
        fetch.assert_called_once_with("CN_Stock", day, require_today=True)
        quote, _ = worker.lookup(symbol, datetime(2026, 11, 25, 12))
        self.assertEqual(quote["quote_status"], "last_quote")

    def test_confirmed_close_accepts_same_timestamp_correction_and_persists(self):
        day = "2026-11-25"
        symbol = "000792.SZ"
        cst = quote_snapshots.CST
        row = {"symbol": symbol, "timestamp": datetime(2026, 11, 25, 15, tzinfo=cst).timestamp(),
               "last_price": 10, "prev_close": 9, "open": 9, "high": 11, "low": 8, "volume": 100}
        client = Mock()
        client.quotes.get.side_effect = [[row], [], [{**row, "volume": 150}], []]
        with tempfile.TemporaryDirectory() as path:
            archive = Path(path) / "close.json.gz"
            worker = quote_snapshots.SnapshotWorker(client, archive)
            with patch.object(quote_snapshots.market_hours, "now", return_value=datetime(2026, 11, 25, 15, 5)), \
                 patch.object(worker.budget, "acquire", return_value=True), \
                 patch("daily_chart_store.current_as_of", return_value=day), \
                 patch("daily_chart_store.final_closes", return_value={symbol: (10, 9)}):
                worker._refresh_close(day, 1)
                first = worker.pools["CN_Stock"][symbol]
                self.assertEqual(first["quote_status"], "close_confirmed")
                worker.archive_dirty = False
                worker._refresh_close(day, 2)
            self.assertTrue(worker.archive_dirty)
            worker._save()
            restarted = quote_snapshots.SnapshotWorker(Mock(), archive)
            restarted._restore()
            quote = restarted.pools["CN_Stock"][symbol]
            self.assertEqual(quote["quote_status"], "close_confirmed")
            self.assertEqual(quote["timestamp"], first["timestamp"])
            self.assertGreater(quote["_revision"], first["_revision"])
            self.assertEqual(quote["volume"], 150)

    def test_truncated_archive_does_not_prevent_first_live_round(self):
        with tempfile.TemporaryDirectory() as path:
            archive = Path(path) / "close.json.gz"
            archive.write_bytes(gzip.compress(b'{"version": 2, "pools": {}, "close_keys": {}}')[:-4])
            worker = quote_snapshots.SnapshotWorker(Mock(), archive)
            now = datetime(2026, 11, 25, 10)
            with patch.object(quote_snapshots.market_hours, "now", return_value=now), \
                 patch.object(quote_snapshots.market_hours, "calendar_source", return_value="xshg"), \
                 patch.object(quote_snapshots.time, "sleep", side_effect=InterruptedError), \
                 patch.object(worker, "_fetch") as fetch, self.assertRaises(InterruptedError):
                worker.run()
            self.assertEqual([call.args[0] for call in fetch.call_args_list], list(quote_snapshots.POOLS))

    def test_partial_pool_failure_close_confirmation_and_restart(self):
        def clock(text):
            return datetime.fromisoformat(text)

        def row(symbol, when, price):
            return {"symbol": symbol, "timestamp": clock(when).replace(tzinfo=quote_snapshots.CST).timestamp(),
                    "last_price": price, "prev_close": 9, "open": 9, "high": 12, "low": 8, "volume": 100}

        symbol = "000792.SZ"
        data = row(symbol, "2026-11-20 15:00", 10)
        client = Mock()
        client.quotes.get.side_effect = [[data], RuntimeError("ETF unavailable"),
                                         [row(symbol, "2026-11-20 14:55", 8)]]
        with tempfile.TemporaryDirectory() as path:
            archive = Path(path) / "close.json.gz"
            worker = quote_snapshots.SnapshotWorker(client, archive)
            close = clock("2026-11-20 15:05")
            with patch.object(quote_snapshots.market_hours, "now", return_value=close), \
                 patch.object(worker.budget, "acquire", return_value=True):
                self.assertTrue(worker._fetch("CN_Stock", "2026-11-20"))
                self.assertFalse(worker._fetch("CN_ETF", "2026-11-20"))
                self.assertTrue(worker._fetch("CN_Stock", "2026-11-20"))
            quote, refresh = worker.lookup(symbol, close)
            self.assertFalse(refresh)
            self.assertEqual(quote["last_price"], 10)
            self.assertEqual(quote["quote_status"], "close_pending")
            with patch("daily_chart_store.current_as_of", return_value="2026-11-20"), \
                 patch("daily_chart_store.final_closes", return_value={symbol: (10, 9)}):
                self.assertTrue(worker._confirm_close("2026-11-20"))
            worker.close_keys["CN_Stock"] = ("2026-11-20", 3)
            worker._save()
            restarted = quote_snapshots.SnapshotWorker(client, archive)
            restarted._restore()
            quote, refresh = restarted.lookup(symbol, clock("2026-11-21 10:00"))
            self.assertFalse(refresh)
            self.assertEqual(quote["quote_trade_date"], "2026-11-20")
            self.assertEqual(quote["quote_status"], "close_confirmed")
            self.assertEqual(restarted.close_keys, {"CN_Stock": ("2026-11-20", 3)})
            self.assertTrue(restarted.lookup(symbol, clock("2026-11-23 09:20"))[1])

    def test_midday_and_close_recovery_retry_only_the_failed_pool(self):
        worker = quote_snapshots.SnapshotWorker(Mock())
        day = "2026-11-25"
        for check, kwargs in ((worker._recover_midday, {"require_today": True}),
                              (lambda day: worker._refresh_close(day, 3), {"after_close": True})):
            with patch.object(worker, "_fetch", side_effect=[True, False, True]) as fetch:
                check(day)
                check(day)
                check(day)
            self.assertEqual([call.args[0] for call in fetch.call_args_list], ["CN_Stock", "CN_ETF", "CN_ETF"])
            self.assertTrue(all(call.kwargs == kwargs for call in fetch.call_args_list))

    def test_yesterdays_close_and_intraday_prices_cannot_confirm_today(self):
        worker = quote_snapshots.SnapshotWorker(Mock())
        stock, etf = "000792.SZ", "510300.SH"
        day = "2026-11-25"
        for symbol, stamp, status in ((stock, "2026-11-24 15:00", "close_confirmed"),
                                      (etf, "2026-11-25 14:58", "live")):
            when = datetime.fromisoformat(stamp).replace(tzinfo=quote_snapshots.CST).timestamp()
            worker.pools["CN_Stock"][symbol] = {"last_price": 10, "prev_close": 9, "timestamp": when,
                                                "snapshot_received_at": when, "quote_trade_date": stamp[:10],
                                                "quote_status": status}
        with patch("daily_chart_store.current_as_of", return_value=day), \
             patch("daily_chart_store.final_closes", return_value={stock: (10, 9), etf: (10, 9)}):
            self.assertFalse(worker._confirm_close(day))
        for symbol in (stock, etf):
            self.assertEqual(worker.lookup(symbol, datetime.fromisoformat(day + " 15:06"))[0]["quote_status"],
                             "close_pending")
        self.assertEqual(worker.lookup(stock, datetime.fromisoformat(day + " 12:00"))[0]["quote_status"], "last_quote")

    def test_old_archive_retries_close_and_downgrades_early_snapshot(self):
        day = "2026-11-25"
        stamp = datetime.fromisoformat(day + " 14:58").replace(tzinfo=quote_snapshots.CST).timestamp()
        symbol = "000792.SZ"
        with tempfile.TemporaryDirectory() as path:
            archive = Path(path) / "close.json.gz"
            with gzip.open(archive, "wt", encoding="utf-8") as handle:
                json.dump({"version": 1, "close_key": [day, 3], "pools": {"CN_Stock": {
                    symbol: {"timestamp": stamp, "snapshot_received_at": stamp, "quote_trade_date": day,
                             "quote_status": "close_confirmed"}}}}, handle)
            worker = quote_snapshots.SnapshotWorker(Mock(), archive)
            worker._restore()
            self.assertEqual(worker.close_keys, {})
            self.assertEqual(worker.pools["CN_Stock"][symbol]["quote_status"], "close_pending")

    def test_pool_info_is_derived_on_read_and_fallback_is_scoped(self):
        worker = Mock()
        quote = {"quote_status": "live", "last_price": 10}
        worker.lookup.return_value = quote, False
        with patch.object(quote_snapshots, "current", return_value=worker), \
             patch.object(market, "_live_info_for_quote", return_value={"atr14": .5}):
            result, missing = market.display_quote_cache(["000792.SZ"])
        self.assertFalse(missing)
        self.assertEqual(result["000792.SZ"]["_live_info"], {"atr14": .5})
        self.assertNotIn("_live_info", quote)
        with patch.dict(market._quote_interests, {object(): {"600000.SH"}}, clear=True), \
             patch.object(market, "_fetch_quotes_locked", return_value={}) as fetch:
            market.fetch_quotes(["000001.SH"], only_requested=True)
        fetch.assert_called_once_with(["000001.SH"], fresh=False)
