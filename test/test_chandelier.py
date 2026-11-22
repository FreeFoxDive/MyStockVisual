# -*- coding: utf-8 -*-
"""吊灯持仓：价位、录入、早盘快照、盘中止损与尾盘收盘线。"""
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

_VISUAL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROOT = os.path.dirname(_VISUAL)
for p in (_VISUAL, _ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import chandelier  # noqa: E402
import monitor  # noqa: E402
import trades  # noqa: E402


def _bar(day, high, low, volume=1000):
    return {"date": day, "high": high, "low": low, "volume": volume}


class LevelTests(unittest.TestCase):
    def test_initial_and_breakeven(self):
        self.assertEqual(chandelier.initial_stop(10, 0.5, "000001.SZ"), 9.25)
        self.assertEqual(chandelier.breakeven_price(10, 0.5), 10.3)
        self.assertEqual(chandelier.initial_stop(10, 0.5, "300750.SZ"), 9.1)
        self.assertEqual(chandelier.initial_stop(10, 0.5, "688001.SH"), 9.1)

    def test_entry_day_high_does_not_arm(self):
        levels = chandelier.compute_levels(
            10, 0.5, "000001.SZ",
            [_bar("2026-11-17", 12, 8.5)],
            "2026-11-18", "2026-11-17",
        )
        self.assertFalse(levels["armed"])
        self.assertEqual(levels["stop"], 9.25)
        self.assertEqual(levels["reason"], "仍是止损")
        self.assertEqual(levels["held"], 1)
        self.assertFalse(levels["expire"])

    def test_arms_from_prior_high_and_trails(self):
        levels = chandelier.compute_levels(
            10, 0.5, "000001.SZ",
            [_bar("2026-11-17", 12, 10.2)],
            "2026-11-18", "2026-11-16",
        )
        self.assertTrue(levels["armed"])
        self.assertEqual(levels["stop"], 10.3)
        self.assertEqual(levels["close"], 10.6)
        self.assertEqual(levels["reason"], "吊灯回撤")
        self.assertEqual(levels["held"], 2)

    def test_twenty_day_low_lifts_the_close_line(self):
        bars = [_bar(f"2026-10-{d:02d}", 10.2, 9.5) for d in range(1, 21)]
        levels = chandelier.compute_levels(
            10, 0.5, "000001.SZ", bars, "2026-10-22", "2026-10-21",
        )
        self.assertFalse(levels["armed"])
        self.assertEqual(levels["close"], 9.5)
        self.assertEqual(levels["reason"], "20日新低")

    def test_close_line_does_not_fall_when_the_window_low_drops(self):
        # 买入前 20 日最低是 9.6，线抬到 9.6。次日下探 9.4 后，线留在 9.6。
        bars = [_bar(f"2026-01-{d:02d}", 10.1, 9.6) for d in range(1, 26)]
        bars.append(_bar("2026-01-26", 10.0, 9.4))
        levels = chandelier.compute_levels(
            10, 0.5, "000001.SZ", bars, "2026-01-27", "2026-01-25",
        )
        self.assertEqual(levels["close"], 9.6)
        self.assertEqual(levels["reason"], "20日新低")
        self.assertEqual(levels["stop"], 9.25)

    def test_carry_forward_keeps_the_higher_line_and_armed_flag(self):
        fresh = {
            "stop": 9.25, "breakeven": 10.1, "armed": False,
            "close": 9.4, "reason": "20日新低", "held": 3, "expire": False,
        }
        kept = chandelier.carry_forward(fresh, {
            "ch_asof": "2026-11-17", "ch_close": 9.6,
            "ch_reason": "20日新低", "ch_armed": 0,
        })
        self.assertEqual(kept["close"], 9.6)
        self.assertEqual(kept["reason"], "20日新低")
        self.assertFalse(kept["armed"])
        armed = chandelier.carry_forward(fresh, {
            "ch_asof": "2026-11-17", "ch_close": 10.2,
            "ch_reason": "吊灯回撤", "ch_armed": 1,
        })
        self.assertTrue(armed["armed"])
        self.assertEqual(armed["stop"], 10.1)
        self.assertEqual(armed["close"], 10.2)
        cleared = chandelier.carry_forward(fresh, {
            "ch_asof": None, "ch_close": 9.6, "ch_armed": 1,
        })
        self.assertEqual(cleared["close"], 9.4)
        self.assertFalse(cleared["armed"])

    def test_halt_does_not_count(self):
        levels = chandelier.compute_levels(
            10, 0.5, "000001.SZ",
            [_bar("2026-11-17", 20, 9, volume=0), _bar("2026-11-17", 10.1, 9.9)],
            "2026-11-18", "2026-11-16",
        )
        self.assertFalse(levels["armed"])
        self.assertEqual(levels["held"], 2)

    def test_fifteenth_traded_day_expires(self):
        bars = [_bar(f"2026-01-{d:02d}", 10.1, 9.9) for d in range(2, 16)]
        levels = chandelier.compute_levels(
            10, 0.5, "000001.SZ", bars, "2026-01-16", "2026-01-01",
        )
        self.assertEqual(levels["held"], 15)
        self.assertTrue(levels["expire"])

    def test_morning_message_skips_take_profit(self):
        text = chandelier.morning_lines([{
            "symbol": "000001.SZ", "name": "平安银行",
            "stop": 9.25, "armed": False, "close": 9.5,
            "reason": "20日新低", "held": 3, "expire": False,
        }], "2026-11-18 09:05")
        self.assertIn("盘中止损9.25", text)
        self.assertIn("保本关", text)
        self.assertIn("收盘线9.50(20日新低)", text)
        self.assertIn("持有3日", text)
        self.assertNotIn("止盈", text)
        self.assertNotIn("量比", text)


class AlertTests(unittest.TestCase):
    def _pos(self, **kw):
        base = {
            "id": 1, "user_id": 1, "symbol": "000001.SZ", "name": "平安银行",
            "exit_mode": "chandelier", "entry_price": 10, "atr_n": 0.5,
            "stop_loss": 9.25, "breakeven": 10.1, "take_profit": None,
            "ch_asof": "2026-11-18", "ch_stop": 9.25, "ch_armed": 0,
            "ch_close": 9.25, "ch_reason": "仍是止损", "ch_held": 2, "ch_expire": 0,
        }
        base.update(kw)
        return base

    def test_intraday_stop_only(self):
        now = datetime(2026, 11, 18, 10, 0)
        pos = self._pos()
        quiet = monitor.evaluate_alerts(pos, {"price": 9.5, "vol_ratio": 3}, now_dt=now)
        self.assertEqual(quiet, [])
        hit = monitor.evaluate_alerts(pos, {"price": 9.2}, now_dt=now)
        self.assertEqual(hit[0]["alert_type"], "ch_stop")
        self.assertIn("初始止损", hit[0]["detail"])
        armed = self._pos(ch_stop=10.1, ch_armed=1)
        be = monitor.evaluate_alerts(armed, {"price": 10.0}, now_dt=now)
        self.assertIn("保本止损", be[0]["detail"])

    def test_gap_through_open(self):
        now = datetime(2026, 11, 18, 9, 31)
        hit = monitor.evaluate_alerts(
            self._pos(), {"price": 9.4, "open": 9.1}, now_dt=now,
        )
        self.assertEqual(hit[0]["alert_type"], "ch_stop")
        self.assertIn("跳空", hit[0]["detail"])

    def test_close_slot_and_expiry(self):
        pos = self._pos(ch_close=9.8, ch_reason="吊灯回撤", ch_stop=9.25)
        early = monitor.evaluate_chandelier_close(pos, 9.7, datetime(2026, 11, 18, 14, 30))
        self.assertEqual(early, [])
        at = monitor.evaluate_chandelier_close(pos, 9.7, datetime(2026, 11, 18, 14, 49))
        self.assertEqual(at[0]["alert_type"], "ch_close_1449")
        self.assertIn("按收盘卖", at[0]["detail"])
        self.assertIn("吊灯回撤", at[0]["detail"])
        still = monitor.evaluate_chandelier_close(pos, 9.7, datetime(2026, 11, 18, 14, 50, 40))
        self.assertEqual(still[0]["alert_type"], "ch_close_1449")
        self.assertEqual(
            monitor.evaluate_chandelier_close(pos, 9.7, datetime(2026, 11, 18, 14, 51)),
            [],
        )
        late = monitor.evaluate_chandelier_close(pos, 9.7, datetime(2026, 11, 18, 14, 55))
        self.assertEqual(late[0]["alert_type"], "ch_close_1454")
        above = monitor.evaluate_chandelier_close(pos, 10.5, datetime(2026, 11, 18, 14, 54))
        self.assertEqual(above, [])
        due = self._pos(ch_expire=1, ch_held=15, ch_close=9.25, ch_reason="仍是止损")
        exp = monitor.evaluate_chandelier_close(due, 10.5, datetime(2026, 11, 18, 14, 54))
        self.assertEqual(exp[0]["alert_type"], "ch_close_1454")
        self.assertIn("今日收盘卖", exp[0]["detail"])

    def test_classic_position_unchanged(self):
        pos = {
            "exit_mode": "", "stop_loss": 9, "breakeven": 10, "take_profit": 11,
            "entry_price": 10,
        }
        out = monitor.evaluate_alerts(
            pos, {"price": 11, "vol_ratio": 1, "prev_price": 10.5},
            now_dt=datetime(2026, 11, 18, 10, 0),
        )
        types = {a["alert_type"] for a in out}
        self.assertIn("tp_reached", types)
        self.assertNotIn("ch_stop", types)

    def test_entry_day_does_not_fire_gap_stop(self):
        pos = self._pos(entry_date="2026-11-18", ch_asof=None)
        now = datetime(2026, 11, 18, 10, 30)
        self.assertEqual(monitor.evaluate_alerts(pos, {"price": 10.0, "open": 9.1}, now_dt=now), [])

    def test_entry_day_skips_close_line(self):
        pos = self._pos(entry_date="2026-11-18", ch_close=9.8, ch_reason="20日新低")
        out = monitor.evaluate_chandelier_close(pos, 9.7, datetime(2026, 11, 18, 14, 49))
        self.assertEqual(out, [])

    def test_legacy_position_without_n_keeps_classic_alerts(self):
        pos = self._pos(atr_n=None, take_profit=11, breakeven=10.5, stop_loss=9)
        out = monitor.evaluate_alerts(
            pos, {"price": 11, "vol_ratio": 1, "prev_price": 10.2},
            now_dt=datetime(2026, 11, 18, 10, 0),
        )
        types = {a["alert_type"] for a in out}
        self.assertIn("tp_reached", types)
        self.assertNotIn("ch_stop", types)

    def test_chandelier_skips_tp_be_and_accel(self):
        pos = self._pos(take_profit=11, breakeven=10.1)
        now = datetime(2026, 11, 18, 10, 30)
        out = monitor.evaluate_alerts(
            pos,
            {"price": 11.2, "prev_price": 10.0, "v60": 0.05, "v60_pm": 0.02,
             "v180": 0.03, "z": 4.0, "vol_ratio": 3.0, "monotone_up": True, "vpm_abs": 0.02},
            limits={"limit_up": 11.0},
            depth={"ask1_vol": 0, "bid1_vol": 100},
            now_dt=now,
        )
        self.assertEqual(out, [])

    def test_poll_once_suppresses_close_after_stop_and_handles_missing_quote(self):
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        trades.init_db(os.path.join(tmp.name, "p.db"))
        trades.PBKDF2_ITERATIONS = 1000
        mid = trades.create_model("吊灯", "", 15, "chandelier")
        uid = trades.create_user("alice", "secret123", is_admin=True)
        with mock.patch("market.get_daily_bar", return_value={
            "date": "2026-11-16", "open": 1, "high": 100, "low": 1, "close": 10, "volume": 1,
        }):
            row = trades.create_trade(uid, {
                "symbol": "000001.SZ", "name": "平安银行", "status": "open",
                "entry_price": 10, "quantity": 100, "entry_date": "2026-11-16",
                "entry_reason": "其他", "model_id": mid, "atr_n": 0.5,
            })
        trades.save_chandelier_snapshot(row["id"], "2026-11-18", {
            "stop": 9.25, "armed": False, "close": 9.8, "reason": "吊灯回撤",
            "held": 2, "expire": False,
        })

        class _FakeFeed:
            def __init__(self, quotes_map):
                self._q = quotes_map
            def instruments(self, syms):
                return {}
            def seed_intraday(self, syms):
                return {}
            def quotes(self, syms):
                return self._q
            def depth(self, syms):
                return {}

        with monitor._lock:
            monitor._buffers.clear()
        with mock.patch.object(monitor.notify_mod, "notify"):
            # 无行情时不能因 metrics is None 崩溃
            self.assertEqual(
                monitor._poll_once(_FakeFeed({}), now_dt=datetime(2026, 11, 18, 10, 0), notify=False),
                [],
            )
            # 盘中触发止损
            f1 = monitor._poll_once(
                _FakeFeed({"000001.SZ": {"last_price": 9.2, "open": 9.5, "volume": 100}}),
                now_dt=datetime(2026, 11, 18, 10, 5), notify=False,
            )
            self.assertEqual([x["alert"]["alert_type"] for x in f1], ["ch_stop"])
            # 14:49 即使反抽到止损上方、收盘线下方，因为盘中已卖不再推送收盘卖
            f2 = monitor._poll_once(
                _FakeFeed({"000001.SZ": {"last_price": 9.6, "open": 9.5, "volume": 200}}),
                now_dt=datetime(2026, 11, 18, 14, 49), notify=False,
            )
            self.assertEqual(f2, [])


class TradeSaveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        trades.init_db(os.path.join(self._tmp.name, "t.db"))
        trades.PBKDF2_ITERATIONS = 1000
        self._bar = mock.patch("market.get_daily_bar", return_value={
            "date": "2026-11-17", "open": 1, "high": 100, "low": 1, "close": 10, "volume": 1,
        })
        self._bar.start()
        self.addCleanup(self._bar.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_model_flag_and_computed_prices(self):
        mid = trades.create_model("吊灯", "", None, "chandelier")
        models = {m["id"]: m for m in trades.list_models(active_only=False)}
        self.assertEqual(models[mid]["exit_mode"], "chandelier")
        self.assertEqual(models[1]["exit_mode"], "")
        uid = trades.create_user("alice", "secret123", is_admin=True)
        with self.assertRaises(trades.TradeValidationError) as cm:
            trades.create_trade(uid, {
                "symbol": "000001.SZ", "name": "平安银行", "status": "open",
                "entry_price": 10, "quantity": 100, "entry_date": "2026-11-17",
                "entry_reason": "其他", "model_id": mid,
            })
        self.assertIn("N", str(cm.exception))
        row = trades.create_trade(uid, {
            "symbol": "000001.SZ", "name": "平安银行", "status": "open",
            "entry_price": 10, "quantity": 100, "entry_date": "2026-11-17",
            "entry_reason": "其他", "model_id": mid, "atr_n": 0.5,
            "take_profit": 99,
        })
        self.assertEqual(row["stop_loss"], 9.25)
        self.assertEqual(row["breakeven"], 10.3)
        self.assertIsNone(row["take_profit"])
        self.assertEqual(row["atr_n"], 0.5)
        self.assertEqual(row["exit_mode"], "chandelier")
        with self.assertRaises(trades.TradeValidationError):
            trades.create_trade(uid, {
                "type": "batch", "symbol": "000001.SZ", "name": "平安银行",
                "model_id": mid, "atr_n": 0.5,
                "legs": [{"side": "buy", "price": 10, "quantity": 100,
                          "date": "2026-11-17", "reason": "其他"}],
            })

    def test_morning_writes_snapshot_once(self):
        mid = trades.create_model("吊灯", "", None, "chandelier")
        uid = trades.create_user("alice", "secret123", is_admin=True)
        row = trades.create_trade(uid, {
            "symbol": "000001.SZ", "name": "平安银行", "status": "open",
            "entry_price": 10, "quantity": 100, "entry_date": "2026-11-16",
            "entry_reason": "其他", "model_id": mid, "atr_n": 0.5,
        })
        bars = [_bar("2026-11-17", 12, 10.2)]
        now = datetime(2026, 11, 18, 9, 5)
        with mock.patch.object(monitor.market_hours, "is_trading_day", return_value=True), \
             mock.patch.object(monitor.notify_mod, "notify") as note:
            first = monitor.check_chandelier_morning(
                now, fetch_bars=lambda symbol, entry: bars, notify=True,
            )
            second = monitor.check_chandelier_morning(
                now, fetch_bars=lambda symbol, entry: bars, notify=True,
            )
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["reason"], "吊灯回撤")
        self.assertEqual(second, [])
        self.assertEqual(note.call_count, 1)
        saved = trades.get_trade(uid, row["id"])
        self.assertEqual(saved["ch_asof"], "2026-11-18")
        self.assertEqual(saved["ch_close"], 10.6)
        self.assertEqual(saved["ch_armed"], 1)
        self.assertIn("吊灯回撤", note.call_args[0][1])
        self.assertNotIn("止盈", note.call_args[0][1])

    def test_hold_expire_excludes_chandelier_and_stale_snapshot_clears(self):
        mid = trades.create_model("吊灯", "", 15, "chandelier")
        uid = trades.create_user("alice", "secret123", is_admin=True)
        row = trades.create_trade(uid, {
            "symbol": "000001.SZ", "name": "平安银行", "status": "open",
            "entry_price": 10, "quantity": 100, "entry_date": "2026-11-16",
            "entry_reason": "其他", "model_id": mid, "atr_n": 0.5,
        })
        self.assertEqual(trades.list_hold_expire_positions(), [])
        trades.save_chandelier_snapshot(row["id"], "2026-11-18", {
            "stop": 10.1, "armed": True, "close": 10.6, "reason": "吊灯回撤",
            "held": 2, "expire": False,
        })
        updated = trades.update_trade(uid, row["id"], {"atr_n": 0.6})
        self.assertIsNone(updated["ch_asof"])
        self.assertEqual(updated["stop_loss"], 9.1)
        with self.assertRaises(trades.TradeValidationError):
            trades.create_trade(uid, {
                "symbol": "000001.SZ", "name": "平安银行", "status": "open",
                "entry_price": 10, "quantity": 100, "entry_date": "2026-11-16",
                "entry_reason": "其他", "model_id": mid, "atr_n": 10,
            })

    def test_morning_failure_does_not_write_snapshot(self):
        mid = trades.create_model("吊灯", "", None, "chandelier")
        uid = trades.create_user("alice", "secret123", is_admin=True)
        row = trades.create_trade(uid, {
            "symbol": "000001.SZ", "name": "平安银行", "status": "open",
            "entry_price": 10, "quantity": 100, "entry_date": "2026-11-16",
            "entry_reason": "其他", "model_id": mid, "atr_n": 0.5,
        })
        now = datetime(2026, 11, 18, 9, 5)
        with mock.patch.object(monitor.market_hours, "is_trading_day", return_value=True):
            out = monitor.check_chandelier_morning(
                now, fetch_bars=lambda s, e: (_ for _ in ()).throw(RuntimeError("boom")),
                notify=False,
            )
        self.assertEqual(out, [])
        self.assertIsNone(trades.get_trade(uid, row["id"])["ch_asof"])

    def test_catch_up_after_the_morning_window(self):
        mid = trades.create_model("吊灯", "", None, "chandelier")
        uid = trades.create_user("alice", "secret123", is_admin=True)
        row = trades.create_trade(uid, {
            "symbol": "000001.SZ", "name": "平安银行", "status": "open",
            "entry_price": 10, "quantity": 100, "entry_date": "2026-11-16",
            "entry_reason": "其他", "model_id": mid, "atr_n": 0.5,
        })
        trades.save_chandelier_snapshot(row["id"], "2026-11-17", {
            "stop": 9.25, "armed": False, "close": 9.6, "reason": "20日新低",
            "held": 1, "expire": False,
        })
        bars = [_bar(f"2026-01-{d:02d}", 10.1, 9.6) for d in range(1, 26)]
        bars.append(_bar("2026-11-17", 10.0, 9.4))
        now = datetime(2026, 11, 18, 10, 30)
        monitor._ch_retry_at.clear()
        with mock.patch.object(monitor.market_hours, "is_trading_day", return_value=True), \
             mock.patch.object(monitor.notify_mod, "notify") as note:
            early = monitor.catch_up_chandelier(
                datetime(2026, 11, 18, 8, 50), fetch_bars=lambda s, e: bars,
            )
            missed = monitor.check_chandelier_morning(now, fetch_bars=lambda s, e: bars)
            caught = monitor.catch_up_chandelier(now, fetch_bars=lambda s, e: bars)
            again = monitor.catch_up_chandelier(now, fetch_bars=lambda s, e: bars)
        self.assertEqual(early, [])
        self.assertEqual(missed, [])
        self.assertEqual(len(caught), 1)
        self.assertEqual(caught[0]["close"], 9.6)
        self.assertEqual(again, [])
        saved = trades.get_trade(uid, row["id"])
        self.assertEqual(saved["ch_asof"], "2026-11-18")
        self.assertEqual(saved["ch_close"], 9.6)
        self.assertEqual(note.call_count, 1)
        self.assertEqual(note.call_args[0][0], "吊灯补算")

    def test_catch_up_retries_a_failure_after_five_minutes(self):
        mid = trades.create_model("吊灯", "", None, "chandelier")
        uid = trades.create_user("alice", "secret123", is_admin=True)
        trades.create_trade(uid, {
            "symbol": "000001.SZ", "name": "平安银行", "status": "open",
            "entry_price": 10, "quantity": 100, "entry_date": "2026-11-16",
            "entry_reason": "其他", "model_id": mid, "atr_n": 0.5,
        })
        calls = {"n": 0}

        def boom(_symbol, _entry):
            calls["n"] += 1
            raise RuntimeError("暂无行情")

        now = datetime(2026, 11, 18, 10, 30)
        monitor._ch_retry_at.clear()
        with mock.patch.object(monitor.market_hours, "is_trading_day", return_value=True):
            monitor.catch_up_chandelier(now, fetch_bars=boom, notify=False)
            monitor.catch_up_chandelier(now, fetch_bars=boom, notify=False)
            self.assertEqual(calls["n"], 1)
            with mock.patch.object(monitor.time, "time", return_value=10**10):
                monitor.catch_up_chandelier(now, fetch_bars=boom, notify=False)
        self.assertEqual(calls["n"], 2)
        monitor._ch_retry_at.clear()

    def test_model_switched_on_keeps_legacy_hold_expire(self):
        mid = trades.create_model("旧模型", "", 15, "")
        uid = trades.create_user("alice", "secret123", is_admin=True)
        trades.create_trade(uid, {
            "symbol": "000001.SZ", "name": "平安银行", "status": "open",
            "entry_price": 10, "quantity": 100, "entry_date": "2026-11-16",
            "entry_reason": "其他", "model_id": mid,
            "take_profit": 12, "breakeven": 11, "stop_loss": 9,
        })
        trades.update_model(mid, "旧模型", "", 15, "chandelier")
        self.assertEqual(trades.list_chandelier_positions(), [])
        held = trades.list_hold_expire_positions()
        self.assertEqual(len(held), 1)
        pos = trades.list_monitored_positions()[0]
        out = monitor.evaluate_alerts(
            pos, {"price": 12, "vol_ratio": 1, "prev_price": 11.5},
            now_dt=datetime(2026, 11, 18, 10, 0),
        )
        self.assertIn("tp_reached", {a["alert_type"] for a in out})


if __name__ == "__main__":
    unittest.main()
