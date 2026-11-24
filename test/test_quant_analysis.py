# -*- coding: utf-8 -*-
"""量化分析引擎与 /api/analysis/quant。

覆盖平仓结算 / 逐日盯市、批次逐腿、费用对齐、跨起始日、用户隔离、
夏普与回撤, 以及接口鉴权和参数校验。

运行:
    venv/Scripts/python.exe -u visual/test/test_quant_analysis.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import quant_analysis  # noqa: E402
import trades  # noqa: E402

trades.PBKDF2_ITERATIONS = 1000

_BAR = {"high": 99999.0, "low": 0.01, "volume": 1_000_000, "open": 10.0, "close": 10.0}


def _bar(symbol, date_str):
    return dict(_BAR, date=str(date_str)[:10])


def _bars(symbol, dates):
    return {str(d)[:10]: dict(_BAR, date=str(d)[:10]) for d in dates}


def _ui_legs(*legs):
    """笔数序第 1…第 N, 转成入库接口要求的「最新在上」。"""
    return list(reversed(legs))


class AnalysisTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = os.path.join(self._tmp.name, "analysis.db")
        trades.init_db(self.db_path)
        trades.PBKDF2_ITERATIONS = 1000
        self._bar_patch = mock.patch("market.get_daily_bar", side_effect=_bar)
        self._bar_patch.start()
        self.addCleanup(self._bar_patch.stop)
        self._bars_patch = mock.patch("market.get_daily_bars", side_effect=_bars)
        self._bars_patch.start()
        self.addCleanup(self._bars_patch.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def _user(self, name="alice"):
        return trades.create_user(name, "secret123")

    def _closed(self, uid, **overrides):
        body = {
            "symbol": "000001.SZ", "name": "平安银行", "status": "closed",
            "entry_price": 10.0, "exit_price": 11.0, "quantity": 100,
            "entry_date": "2026-01-05", "exit_date": "2026-01-07",
            "entry_reason": "突破买入", "exit_reason": "止盈(达到目标价)",
        }
        body.update(overrides)
        return trades.create_trade(uid, body)

    def _batch(self, uid, legs, **overrides):
        body = {"type": "batch", "symbol": "000001.SZ", "name": "平安银行", "legs": _ui_legs(*legs)}
        body.update(overrides)
        return trades.create_trade(uid, body)

    def _run(self, uid, **kwargs):
        kwargs.setdefault("today", "2026-01-09")
        kwargs.setdefault("deduct_fees", False)
        return quant_analysis.compute_quant_analysis(uid, **kwargs)


class TestModelPositionStats(AnalysisTestCase):
    def test_only_selected_closed_trades_and_same_fee_basis_as_quant(self):
        uid = self._user()
        other_uid = self._user("bob")
        mid, other_mid = [m["id"] for m in trades.list_models(active_only=True)[:2]]
        self._closed(uid, model_id=mid, exit_price=12)
        self._batch(uid, [
            {"side": "buy", "price": 10, "quantity": 100, "date": "2026-01-05"},
            {"side": "sell", "price": 9, "quantity": 100, "date": "2026-01-07"},
        ], model_id=mid)
        self._batch(uid, [
            {"side": "buy", "price": 10, "quantity": 200, "date": "2026-01-05"},
            {"side": "sell", "price": 15, "quantity": 100, "date": "2026-01-07"},
        ], model_id=mid)
        self._closed(uid, model_id=other_mid, exit_price=9)
        self._closed(uid, exit_price=9)
        self._closed(other_uid, model_id=mid, exit_price=9)
        full = self._run(uid, model_id=mid, deduct_fees=True)["summary"]
        with mock.patch.object(quant_analysis, "_analyze", side_effect=AssertionError("full report")):
            light = quant_analysis.compute_model_position_stats(uid, str(mid))
            unassigned = quant_analysis.compute_model_position_stats(uid, "none")
        self.assertEqual(light["closed_count"], 2)
        self.assertEqual(light["win_rate"], 50)
        self.assertEqual(light["closed_count"], full["closed_count"])
        self.assertEqual(round(light["win_rate"], 2), full["win_rate"])
        self.assertEqual(round(light["payoff_ratio"], 2), full["payoff_ratio"])
        self.assertEqual(unassigned, {"closed_count": 1, "win_rate": 0, "payoff_ratio": None})

    def test_position_stats_keep_precision_near_break_even(self):
        uid = self._user()
        with mock.patch.object(trades, "get_user_fees", return_value=None):
            for _ in range(22):
                self._closed(uid, exit_price=10.136)
            for _ in range(3):
                self._closed(uid, exit_price=9)
            summary = quant_analysis.compute_model_position_stats(uid, "none")
        self.assertEqual(summary["closed_count"], 25)
        self.assertAlmostEqual(summary["win_rate"], 88)
        self.assertAlmostEqual(summary["payoff_ratio"], 0.136)
        p = summary["win_rate"] / 100
        self.assertLess(p - (1 - p) / summary["payoff_ratio"], 0)

    def test_position_stats_do_not_round_fractional_win_rate(self):
        uid = self._user()
        with mock.patch.object(trades, "get_user_fees", return_value=None):
            self._closed(uid, exit_price=10.5)
            self._closed(uid, exit_price=10.5)
            self._closed(uid, exit_price=9)
            summary = quant_analysis.compute_model_position_stats(uid, "none")
        self.assertAlmostEqual(summary["win_rate"], 200 / 3)
        self.assertEqual(summary["payoff_ratio"], 0.5)


class TestRealizedCurve(AnalysisTestCase):
    def test_simple_pnl_lands_on_exit_day_and_position_clears(self):
        uid = self._user()
        self._closed(uid, entry_price=10, exit_price=12, quantity=1000,
                     entry_date="2026-01-05", exit_date="2026-01-07")
        out = self._run(uid, start="2026-01-05", end="2026-01-07", capital=100000)
        curve = {day: eq for day, eq in zip(out["curve"]["dates"], out["curve"]["equity"])}
        pos = {day: pct for day, pct in zip(out["curve"]["dates"], out["curve"]["pos_pct"])}
        self.assertEqual(curve["2026-01-05"], 100000)
        self.assertEqual(curve["2026-01-06"], 100000)
        self.assertEqual(curve["2026-01-07"], 102000)
        self.assertEqual(out["summary"]["total_pnl"], 2000)
        self.assertEqual(out["summary"]["end_nav"], 1.02)
        self.assertAlmostEqual(pos["2026-01-05"], 10.0, places=2)
        self.assertAlmostEqual(pos["2026-01-06"], 10.0, places=2)
        self.assertEqual(pos["2026-01-07"], 0)
        self.assertEqual(out["curve"]["hold_count"][0], 1)
        self.assertEqual(out["summary"]["closed_count"], 1)
        self.assertEqual(out["summary"]["win_rate"], 100)

    def test_batch_sell_legs_sum_to_official_pnl(self):
        uid = self._user()
        legs = [
            {"side": "buy", "price": 10.0, "quantity": 1000, "date": "2026-01-05", "time": "09:30"},
            {"side": "buy", "price": 12.0, "quantity": 500, "date": "2026-01-06", "time": "09:30"},
            {"side": "sell", "price": 15.0, "quantity": 800, "date": "2026-01-07", "time": "14:00", "reason": "减仓"},
            {"side": "sell", "price": 16.0, "quantity": 700, "date": "2026-01-08", "time": "14:00", "reason": "清仓"},
        ]
        created = self._batch(uid, legs)
        stored = trades.get_trade(uid, created["id"])
        official = trades._trade_metrics(
            stored, fee_config=trades.get_user_fees(uid), deduct_fees=True,
        )["pnl"]
        out = self._run(uid, start="2026-01-05", end="2026-01-08", capital=100000, deduct_fees=True)
        self.assertEqual(out["summary"]["total_pnl"], official)
        self.assertEqual(out["summary"]["closed_count"], 1)
        self.assertEqual(out["summary"]["by_exit_reason"] if False else out["by_exit_reason"][0]["reason"], "清仓")
        self.assertEqual(out["trades"][0]["leg_count"], 4)
        self.assertEqual(out["trades"][0]["type"], "batch")

    def test_batch_without_fees_splits_across_sell_days(self):
        uid = self._user()
        legs = [
            {"side": "buy", "price": 10.0, "quantity": 1000, "date": "2026-01-05", "time": "09:30"},
            {"side": "buy", "price": 12.0, "quantity": 500, "date": "2026-01-06", "time": "09:30"},
            {"side": "sell", "price": 15.0, "quantity": 800, "date": "2026-01-07", "time": "14:00"},
            {"side": "sell", "price": 16.0, "quantity": 700, "date": "2026-01-08", "time": "14:00"},
        ]
        self._batch(uid, legs)
        out = self._run(uid, start="2026-01-05", end="2026-01-08")
        curve = dict(zip(out["curve"]["dates"], out["curve"]["equity"]))
        self.assertEqual(out["summary"]["total_pnl"], 7200)
        self.assertAlmostEqual(curve["2026-01-07"] - curve["2026-01-06"], 3466.67, places=2)
        self.assertAlmostEqual(curve["2026-01-08"] - curve["2026-01-07"], 3733.33, places=2)

    def test_open_batch_partial_sell_is_in_nav_not_win_rate(self):
        uid = self._user()
        legs = [
            {"side": "buy", "price": 10.0, "quantity": 1000, "date": "2026-01-05", "time": "09:30"},
            {"side": "buy", "price": 12.0, "quantity": 500, "date": "2026-01-06", "time": "09:30"},
            {"side": "sell", "price": 15.0, "quantity": 800, "date": "2026-01-07", "time": "14:00"},
        ]
        created = self._batch(uid, legs)
        self.assertEqual(created["status"], "open")
        out = self._run(uid, start="2026-01-05", end="2026-01-08")
        self.assertEqual(out["summary"]["total_pnl"], created["pnl"])
        self.assertEqual(out["summary"]["closed_count"], 0)
        self.assertEqual(out["summary"]["open_count"], 1)
        self.assertIsNone(out["summary"]["win_rate"])
        self.assertGreater(out["summary"]["open_partial_pnl"], 0)
        self.assertTrue(any("未计入胜率" in note for note in out["notes"]))
        # 减仓后还剩 700 股, 均价约 10.6667
        pos = dict(zip(out["curve"]["dates"], out["curve"]["pos_pct"]))
        self.assertGreater(pos["2026-01-08"], 0)
        self.assertLess(pos["2026-01-08"], pos["2026-01-06"])

    def test_open_batch_does_not_book_later_buy_fees_on_earlier_sell(self):
        uid = self._user()
        legs = [
            {"side": "buy", "price": 10.0, "quantity": 1000, "date": "2026-01-05", "time": "09:30"},
            {"side": "sell", "price": 12.0, "quantity": 1000, "date": "2026-01-06", "time": "14:00"},
            {"side": "buy", "price": 11.0, "quantity": 1000, "date": "2026-01-08", "time": "09:30"},
        ]
        self._batch(uid, legs)
        fees = trades.get_user_fees(uid)
        buy1 = trades._calc_side_fees("000001.SZ", "buy", 10.0, 1000, fees)["total"]
        sell = trades._calc_side_fees("000001.SZ", "sell", 12.0, 1000, fees)["total"]
        out = self._run(uid, start="2026-01-05", end="2026-01-06", deduct_fees=True)
        self.assertAlmostEqual(out["summary"]["total_pnl"], round(2000 - buy1 - sell, 2), places=2)

    def test_window_starts_after_first_sell_keeps_only_later_leg(self):
        uid = self._user()
        legs = [
            {"side": "buy", "price": 10.0, "quantity": 1000, "date": "2026-01-05", "time": "09:30"},
            {"side": "buy", "price": 12.0, "quantity": 500, "date": "2026-01-06", "time": "09:30"},
            {"side": "sell", "price": 15.0, "quantity": 800, "date": "2026-01-07", "time": "14:00"},
            {"side": "sell", "price": 16.0, "quantity": 700, "date": "2026-01-08", "time": "14:00"},
        ]
        self._batch(uid, legs)
        out = self._run(uid, start="2026-01-08", end="2026-01-08")
        self.assertEqual(out["summary"]["total_pnl"], 3733.33)
        self.assertEqual(out["summary"]["closed_count"], 1)

    def test_same_day_round_trip_counts_as_t(self):
        uid = self._user()
        legs = [
            {"side": "buy", "price": 10.0, "quantity": 500, "date": "2026-01-05", "time": "09:30"},
            {"side": "sell", "price": 10.5, "quantity": 500, "date": "2026-01-05", "time": "14:00"},
        ]
        self._batch(uid, legs)
        out = self._run(uid, start="2026-01-05", end="2026-01-05")
        self.assertEqual(out["summary"]["total_pnl"], 250)
        self.assertEqual(out["summary"]["t_stats"]["count"], 1)
        self.assertEqual(out["summary"]["t_stats"]["positive"], 1)
        self.assertEqual(out["summary"]["t_stats"]["success"], 1)
        self.assertEqual(out["curve"]["pos_pct"][-1], 0)


class TestMarkToMarket(AnalysisTestCase):
    def _closes(self, mapping):
        def fetcher(symbol, dates):
            return {day: {"close": mapping[day]} for day in dates if day in mapping}
        return fetcher

    def test_prewindow_position_uses_previous_close_as_baseline(self):
        uid = self._user()
        self._closed(uid, status="open", exit_price=None, exit_date=None, exit_reason=None,
                     entry_price=10, quantity=100, entry_date="2026-01-05")
        fetcher = self._closes({
            "2026-01-06": 12, "2026-01-07": 13, "2026-01-08": 11, "2026-01-09": 14,
        })
        out = self._run(
            uid, start="2026-01-07", end="2026-01-09", mode="mtm",
            capital=100000, bar_fetcher=fetcher,
        )
        curve = dict(zip(out["curve"]["dates"], out["curve"]["equity"]))
        self.assertEqual(out["curve"]["dates"][0], "2026-01-07")
        self.assertEqual(curve["2026-01-07"], 100100)
        self.assertEqual(curve["2026-01-09"], 100200)
        self.assertEqual(out["summary"]["total_pnl"], 200)
        self.assertFalse(out["warnings"])

    def test_missing_close_locf_and_cost_fallback_warn(self):
        uid = self._user()
        self._closed(uid, status="open", exit_price=None, exit_date=None, exit_reason=None,
                     symbol="600000.SH", name="浦发银行",
                     entry_price=10, quantity=100, entry_date="2026-01-05")
        fetcher = self._closes({"2026-01-06": 12, "2026-01-08": 12})
        out = self._run(
            uid, start="2026-01-07", end="2026-01-08", mode="mtm",
            bar_fetcher=fetcher, capital=100000,
        )
        curve = dict(zip(out["curve"]["dates"], out["curve"]["equity"]))
        self.assertEqual(curve["2026-01-07"], 100000)
        self.assertTrue(any("600000.SH" in item for item in out["warnings"]))

        bare = self._run(
            uid, start="2026-01-07", end="2026-01-07", mode="mtm",
            bar_fetcher=lambda symbol, dates: {}, capital=100000,
        )
        self.assertEqual(bare["summary"]["total_pnl"], 0)
        self.assertTrue(bare["warnings"])

    def test_mtm_without_fetcher_uses_one_batch(self):
        uid = self._user()
        self._closed(uid, status="open", exit_price=None, exit_date=None, exit_reason=None,
                     entry_price=10, quantity=100, entry_date="2026-01-05")
        seen = {}

        def many(symbol_dates):
            seen.update(symbol_dates)
            return {
                symbol: {day: {"date": day, "close": 12.0, "high": 13.0, "low": 11.0} for day in days}
                for symbol, days in symbol_dates.items()
            }

        with mock.patch("market.get_daily_bars_many", side_effect=many) as batched:
            out = self._run(uid, start="2026-01-05", end="2026-01-06", mode="mtm", capital=100000)
        batched.assert_called_once()
        self.assertIn("000001.SZ", seen)
        self.assertEqual(out["summary"]["total_pnl"], 200)

    def test_mtm_only_fetches_days_each_symbol_is_held(self):
        uid = self._user()
        self._closed(uid, entry_date="2026-01-05", exit_date="2026-01-06")
        asked = {}

        def fetcher(symbol, dates):
            asked[symbol] = list(dates)
            return {day: dict(_BAR, date=day, close=10.5) for day in dates}

        out = self._run(uid, start="2026-01-05", end="2026-01-09", mode="mtm", bar_fetcher=fetcher)
        self.assertEqual(asked["000001.SZ"], ["2026-01-05", "2026-01-06"])
        self.assertFalse(out["warnings"])
        self.assertEqual(out["summary"]["total_pnl"], 100)


class TestStats(AnalysisTestCase):
    def test_drawdown_sharpe_and_streaks(self):
        uid = self._user()
        self._closed(uid, symbol="000001.SZ", name="平安银行",
                     entry_price=10, exit_price=20, quantity=1000,
                     entry_date="2026-01-05", exit_date="2026-01-06",
                     exit_reason="止盈(达到目标价)")
        self._closed(uid, symbol="000002.SZ", name="万科A",
                     entry_price=20, exit_price=9, quantity=1000,
                     entry_date="2026-01-07", exit_date="2026-01-08",
                     exit_reason="止损(跌破止损位)")
        out = self._run(uid, start="2026-01-05", end="2026-01-09", capital=100000)
        self.assertEqual(out["summary"]["max_dd_pct"], 10)
        self.assertEqual(out["summary"]["max_dd_date"], "2026-01-08")
        self.assertEqual(out["summary"]["max_dd_peak"], "2026-01-07")
        self.assertEqual(out["summary"]["max_dd_days"], 2)
        self.assertEqual(out["summary"]["dd_start"], "2026-01-08")
        self.assertEqual(out["summary"]["dd_end"], "2026-01-09")
        self.assertEqual(out["summary"]["max_win_streak"], 1)
        self.assertEqual(out["summary"]["max_loss_streak"], 1)
        self.assertAlmostEqual(out["summary"]["profit_factor"], round(10000 / 11000, 2))
        self.assertIsNotNone(out["summary"]["sharpe"])
        self.assertIsNotNone(out["summary"]["sortino"])
        self.assertIsNotNone(out["summary"]["calmar"])
        nav = out["curve"]["nav"]
        rets = [nav[0] - 1.0]
        for i in range(1, len(nav)):
            rets.append(nav[i] / nav[i - 1] - 1.0)
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        sharpe = mean / math.sqrt(var) * math.sqrt(252)
        self.assertEqual(out["summary"]["sharpe"], round(sharpe, 2))
        # 3% 不会出现; 这两笔一个大赢一个大亏, 直方图两端有数
        hist = dict(zip(out["return_hist"]["bins"], out["return_hist"]["counts"]))
        self.assertEqual(sum(hist.values()), 2)

    def test_monthly_return_matches_equity_change(self):
        uid = self._user()
        self._closed(uid, entry_price=10, exit_price=11, quantity=1000,
                     entry_date="2026-01-05", exit_date="2026-01-06")
        self._closed(uid, symbol="000002.SZ", name="万科A",
                     entry_price=10, exit_price=9, quantity=1000,
                     entry_date="2026-02-02", exit_date="2026-02-03")
        out = self._run(uid, start="2026-01-05", end="2026-02-03", capital=100000, today="2026-02-03")
        months = {row["month"]: row for row in out["monthly"]}
        self.assertEqual(months["2026-01"]["pnl"], 1000)
        self.assertEqual(months["2026-02"]["pnl"], -1000)
        self.assertEqual(months["2026-01"]["count"], 1)
        self.assertAlmostEqual(months["2026-01"]["return_pct"], 1.0, places=2)

    def test_model_filter_isolation_and_repo_skipped(self):
        uid = self._user()
        other = self._user("bob")
        models = trades.list_models(active_only=True)
        a_id, b_id = models[0]["id"], models[1]["id"]
        self._closed(uid, model_id=a_id, exit_price=12, quantity=100)
        self._closed(uid, symbol="000002.SZ", name="万科A", model_id=b_id, exit_price=9, quantity=100)
        self._closed(other, exit_price=50, quantity=1000)
        trades.create_trade(uid, {
            "type": "reverse_repo", "symbol": "204001.SH", "name": "GC001",
            "entry_price": 3.0, "quantity": 100000, "entry_date": "2026-01-05",
            "entry_reason": "其他",
        })
        only_a = self._run(uid, model_id=a_id, start="2026-01-05", end="2026-01-09")
        self.assertEqual(only_a["summary"]["closed_count"], 1)
        self.assertEqual(only_a["summary"]["total_pnl"], 200)
        everything = self._run(uid, start="2026-01-05", end="2026-01-09")
        self.assertEqual(everything["summary"]["closed_count"], 2)
        self.assertNotEqual(everything["summary"]["total_pnl"], only_a["summary"]["total_pnl"])
        compared = self._run(uid, start="2026-01-05", end="2026-01-09", compare=True)
        names = {row["name"] for row in compared["model_comparison"]}
        self.assertIn(models[0]["name"], names)
        self.assertIn(models[1]["name"], names)
        self.assertTrue(all(len(row["nav"]) == len(compared["curve"]["nav"]) for row in compared["model_comparison"]))

    def test_position_over_100_is_kept_and_noted(self):
        uid = self._user()
        self._closed(uid, status="open", exit_price=None, exit_date=None, exit_reason=None,
                     entry_price=10, quantity=1000, entry_date="2026-01-05")
        out = self._run(uid, start="2026-01-05", end="2026-01-06", capital=1000)
        self.assertGreater(out["summary"]["max_position_pct"], 100)
        self.assertTrue(any(v > 100 for v in out["curve"]["pos_pct"]))
        self.assertTrue(any("峰值仓位超过 100%" in note for note in out["notes"]))
        self.assertTrue(any("持仓成本相对初始资金" in note for note in out["notes"]))

    def test_mtm_position_over_100_when_cost_exceeds_capital(self):
        uid = self._user()
        self._closed(uid, status="open", exit_price=None, exit_date=None, exit_reason=None,
                     entry_price=10, quantity=1000, entry_date="2026-01-05")
        closes = {"2026-01-05": 10.0, "2026-01-06": 5.0}

        def fetcher(symbol, dates):
            return {day: dict(_BAR, date=day, close=closes[day]) for day in dates if day in closes}

        # 成本 1 万 <= 资金 1.2 万: 跌一半后 市值 5000 / 权益 7000, 不超
        out = self._run(uid, start="2026-01-05", end="2026-01-06", mode="mtm",
                        capital=12000, bar_fetcher=fetcher)
        self.assertLessEqual(out["summary"]["max_position_pct"], 100)
        self.assertFalse(any("峰值仓位超过 100%" in note for note in out["notes"]))

        # 成本 1 万 > 资金 8000: 1/5 市值 10000 / 权益 8000, 1/6 市值 5000 / 权益 3000
        out = self._run(uid, start="2026-01-05", end="2026-01-06", mode="mtm",
                        capital=8000, bar_fetcher=fetcher)
        pos = dict(zip(out["curve"]["dates"], out["curve"]["pos_pct"]))
        self.assertEqual(pos["2026-01-05"], 125.0)
        self.assertEqual(pos["2026-01-06"], round(5000 / 3000 * 100, 2))
        self.assertTrue(any("持仓市值相对当日权益" in note for note in out["notes"]))

    def test_empty_and_invalid_inputs(self):
        uid = self._user()
        out = self._run(uid, start="2026-01-05", end="2026-01-06", capital=100000)
        self.assertEqual(out["summary"]["total_pnl"], 0)
        self.assertEqual(out["summary"]["end_nav"], 1)
        self.assertIsNone(out["summary"]["sharpe"])
        with self.assertRaises(quant_analysis.AnalysisError) as ctx:
            self._run(uid, capital=10)
        self.assertIn("初始资金", ctx.exception.message)
        with self.assertRaises(quant_analysis.AnalysisError):
            self._run(uid, start="2026-02-01", end="2026-01-01")
        with self.assertRaises(quant_analysis.AnalysisError):
            self._run(uid, mode="mark")
        with self.assertRaises(quant_analysis.AnalysisError) as ctx:
            self._run(uid, start="2026-01-05", end="2026-01-12")
        self.assertIn("今天", ctx.exception.message)
        with self.assertRaises(quant_analysis.AnalysisError):
            self._run(uid, start="0001-01-01", end="2026-01-09")


class TestAnalysisApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "analysis_api.db"
        cls._orig_db = trades._db_path
        cls._orig_iter = trades.PBKDF2_ITERATIONS
        trades.PBKDF2_ITERATIONS = 1000
        trades.init_db(cls._db)
        trades.create_user("analysis_user", "password123")
        from app import create_app
        cls.app = create_app()
        cls.client = cls.app.test_client()
        cls.client.get("/login.html")
        token = cls.client.get_cookie("csrf_token")
        headers = {"X-CSRF-Token": token.value if hasattr(token, "value") else str(token)}
        logged = cls.client.post(
            "/api/auth/login",
            data=json.dumps({"username": "analysis_user", "password": "password123"}),
            content_type="application/json", headers=headers,
        )
        assert logged.status_code == 200, logged.data
        cls._bar = mock.patch("market.get_daily_bar", side_effect=_bar)
        cls._bar.start()

    @classmethod
    def tearDownClass(cls):
        cls._bar.stop()
        trades._db_path = cls._orig_db
        trades.PBKDF2_ITERATIONS = cls._orig_iter
        cls._tmpdir.cleanup()

    def test_requires_login(self):
        anon = self.app.test_client()
        response = anon.get("/api/analysis/quant")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(anon.get("/api/analysis/model-position-stats?model_id=none").status_code, 401)

    def test_position_stats_route_checks_model_and_returns_only_stats(self):
        url = "/api/analysis/model-position-stats?model_id="
        self.assertEqual(self.client.get(url + "all").status_code, 400)
        response = self.client.get(url + "2147483647")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.get_json(), {
            "summary": {"closed_count": 0, "win_rate": None, "payoff_ratio": None},
        })

    def test_position_stats_cache_reuses_only_same_account_and_model_until_expiry(self):
        cache = self.app.extensions["model_position_stats_cache"]
        cache.clear()
        self.addCleanup(cache.clear)
        url = "/api/analysis/model-position-stats?model_id="
        user = {"id": 1001}
        from market import time
        with mock.patch("app.current_user_from_request", return_value=user), \
             mock.patch.object(trades, "get_user_fees", wraps=trades.get_user_fees) as fees, \
             mock.patch.object(time, "time", return_value=time.time()) as clock:
            self.assertEqual(self.client.get(url + "42").status_code, 200)
            self.assertEqual(self.client.get(url + "042").status_code, 200)
            self.assertEqual(fees.call_count, 1)
            self.assertEqual(self.client.get(url + "none").status_code, 200)
            self.assertEqual(fees.call_count, 2)
            user["id"] = 1002
            self.assertEqual(self.client.get(url + "42").status_code, 200)
            self.assertEqual(fees.call_count, 3)
            clock.return_value += 16
            self.assertEqual(self.client.get(url + "42").status_code, 200)
            self.assertEqual(fees.call_count, 4)

    def test_rejects_bad_params(self):
        for query, message in (
            ("capital=10", "初始资金"),
            ("from=2026-02-01&to=2026-01-01", "起始日期不能晚于结束日期"),
            ("mode=mark", "净值口径无效"),
            ("model_id=abc", "模型参数无效"),
        ):
            response = self.client.get("/api/analysis/quant?" + query)
            self.assertEqual(response.status_code, 400, query)
            self.assertIn(message, response.get_json()["error"])

    def test_returns_curve_for_account(self):
        created = self.client.post(
            "/api/trades",
            data=json.dumps({
                "symbol": "600000.SH", "name": "浦发银行", "status": "closed",
                "entry_price": 10, "exit_price": 11, "quantity": 100,
                "entry_date": "2026-01-05", "exit_date": "2026-01-07",
                "entry_reason": "突破买入", "exit_reason": "止盈(达到目标价)",
            }),
            content_type="application/json",
            headers=self._csrf(),
        )
        self.assertEqual(created.status_code, 201, created.data)
        response = self.client.get(
            "/api/analysis/quant?from=2026-01-05&to=2026-01-09&capital=100000&mode=realized&deduct_fees=0"
        )
        self.assertEqual(response.status_code, 200, response.data)
        body = response.get_json()
        for key in ("summary", "curve", "monthly", "return_hist", "by_exit_reason", "trades"):
            self.assertIn(key, body)
        self.assertGreaterEqual(body["summary"]["total_pnl"], 100)
        self.assertEqual(body["curve"]["dates"][0][:4], "2026")

    def _csrf(self):
        self.client.get("/login.html")
        token = self.client.get_cookie("csrf_token")
        return {"X-CSRF-Token": token.value if hasattr(token, "value") else str(token)}


if __name__ == "__main__":
    unittest.main()
