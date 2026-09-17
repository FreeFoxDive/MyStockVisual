# -*- coding: utf-8 -*-
"""market_hours.session_phase 边界测试 (唯一时段口径)。

运行:
    venv/Scripts/python.exe -u visual/test/test_market_hours.py
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import market_hours  # noqa: E402


class MarketClockTest(unittest.TestCase):
    def test_utc_previous_day_becomes_shanghai_today(self):
        instant = datetime(2026, 9, 14, 17, 14, tzinfo=timezone.utc)
        with mock.patch.object(market_hours, "datetime") as clock:
            clock.now.side_effect = lambda tz: instant.astimezone(tz)
            actual = market_hours.now()
        self.assertEqual(actual, datetime(2026, 9, 15, 1, 14))
        self.assertIsNone(actual.tzinfo)  # existing naive Shanghai wall-clock contract
        clock.now.assert_called_once_with(market_hours._CST)


class SessionPhaseTest(unittest.TestCase):
    def _phase(self, when: str, trading=True):
        dt = datetime.strptime(when, "%Y-%m-%d %H:%M")
        with mock.patch.object(market_hours, "is_trading_day", return_value=trading):
            return market_hours.session_phase(dt)

    def test_non_trading_day(self):
        self.assertEqual(self._phase("2026-09-13 10:00", trading=False), "non_trading")

    def test_pre_open(self):
        self.assertEqual(self._phase("2026-09-14 09:14"), "pre")
        self.assertEqual(self._phase("2026-09-14 00:05"), "pre")

    def test_call_auction(self):
        # 09:15-09:30 集合竞价: 快照已有效 (虚拟匹配价), 但当日 bar 未成型
        self.assertEqual(self._phase("2026-09-14 09:15"), "auction")
        self.assertEqual(self._phase("2026-09-14 09:25"), "auction")
        self.assertEqual(self._phase("2026-09-14 09:29"), "auction")

    def test_trading_morning_and_afternoon(self):
        self.assertEqual(self._phase("2026-09-14 09:30"), "trading")
        self.assertEqual(self._phase("2026-09-14 11:30"), "trading")
        self.assertEqual(self._phase("2026-09-14 13:00"), "trading")
        self.assertEqual(self._phase("2026-09-14 15:00"), "trading")

    def test_lunch_break(self):
        self.assertEqual(self._phase("2026-09-14 11:31"), "break")
        self.assertEqual(self._phase("2026-09-14 12:59"), "break")

    def test_closed(self):
        self.assertEqual(self._phase("2026-09-14 15:01"), "closed")
        self.assertEqual(self._phase("2026-09-14 23:00"), "closed")

    def test_bar_ready_phases_exclude_pre_and_auction(self):
        self.assertIn("trading", market_hours.BAR_READY_PHASES)
        self.assertIn("break", market_hours.BAR_READY_PHASES)
        self.assertIn("closed", market_hours.BAR_READY_PHASES)
        for phase in ("pre", "auction", "non_trading"):
            self.assertNotIn(phase, market_hours.BAR_READY_PHASES,
                             f"{phase} 的当日 bar 未成型, 拼出来是上一交易日的残留")


class LiveGateTest(unittest.TestCase):
    """is_live 是"要不要拉行情"的唯一口径; in_session 仍只表示连续竞价。"""

    def _gates(self, when: str, trading=True):
        dt = datetime.strptime(when, "%Y-%m-%d %H:%M")
        with mock.patch.object(market_hours, "is_trading_day", return_value=trading):
            return (market_hours.is_live(dt), market_hours.in_session(dt),
                    market_hours.can_connect_stream(dt), market_hours.is_auction(dt))

    def test_truth_table(self):
        cases = {
            "2026-09-14 08:59": (False, False, False, False),
            "2026-09-14 09:00": (False, False, True, False),   # 提前建流窗口
            "2026-09-14 09:15": (True, False, True, True),     # 竞价: 取数但不建当日 bar
            "2026-09-14 09:29": (True, False, True, True),
            "2026-09-14 09:30": (True, True, True, False),
            "2026-09-14 11:30": (True, True, True, False),
            "2026-09-14 11:31": (False, False, True, False),   # 午休: 连接保留, 不取数
            "2026-09-14 13:00": (True, True, True, False),
            "2026-09-14 15:00": (True, True, True, False),
            "2026-09-14 15:01": (False, False, False, False),  # 收盘: 连流也停, 不整夜占线程
            "2026-09-14 23:00": (False, False, False, False),
        }
        for when, expected in cases.items():
            with self.subTest(when=when):
                self.assertEqual(self._gates(when), expected)

    def test_non_trading_day_blocks_everything(self):
        self.assertEqual(self._gates("2026-09-13 10:00", trading=False),
                         (False, False, False, False))

    def test_in_session_semantics_unchanged(self):
        """in_session 仍是"连续竞价"—— 量比/停顿判定/监控告警都依赖它。"""
        self.assertFalse(self._gates("2026-09-14 09:20")[1], "竞价不算 in_session")
        self.assertTrue(self._gates("2026-09-14 10:00")[1])
        self.assertFalse(self._gates("2026-09-14 12:00")[1], "午休不算 in_session")


class NextOpenTest(unittest.TestCase):
    """下一次开盘/开始取数的时刻 (长假与跨日都算准)。"""

    def test_intraday_targets(self):
        # 盘前 → 今日 09:15 起有数据, 09:30 开盘
        pre = datetime(2026, 9, 14, 8, 0)
        self.assertEqual(market_hours.next_live_at(pre), datetime(2026, 9, 14, 9, 15))
        self.assertEqual(market_hours.next_session_open(pre), datetime(2026, 9, 14, 9, 30))
        # 午休 → 今日 13:00
        brk = datetime(2026, 9, 14, 12, 0)
        self.assertEqual(market_hours.next_live_at(brk), datetime(2026, 9, 14, 13, 0))
        self.assertEqual(market_hours.next_session_open(brk), datetime(2026, 9, 14, 13, 0))
        # 盘中 → 没有"下一个"
        trading = datetime(2026, 9, 14, 10, 0)
        self.assertIsNone(market_hours.next_live_at(trading))
        self.assertIsNone(market_hours.next_session_open(trading))

    def test_after_close_rolls_to_next_trading_day(self):
        # 2026-09-18 是周五 → 下一交易日是 09-21 周一
        fri = datetime(2026, 9, 18, 16, 0)
        self.assertEqual(market_hours.next_live_at(fri), datetime(2026, 9, 21, 9, 15))
        self.assertEqual(market_hours.next_session_open(fri), datetime(2026, 9, 21, 9, 30))
        self.assertEqual(market_hours.next_stream_at(fri), datetime(2026, 9, 21, 9, 0))

    def test_weekend(self):
        sat = datetime(2026, 9, 19, 10, 0)
        self.assertEqual(market_hours.next_session_open(sat), datetime(2026, 9, 21, 9, 30))
        self.assertEqual(market_hours.next_stream_at(sat), datetime(2026, 9, 21, 9, 0))
        self.assertFalse(market_hours.can_connect_stream(sat), "周末不建流")

    def test_long_holiday_beyond_old_ten_day_scan(self):
        """春节长假 (含前后周末 10 天) 曾让旧实现的 10 天扫描静默放弃。"""
        before = datetime(2026, 2, 13, 16, 0)
        nxt = market_hours.next_session_open(before)
        self.assertEqual(nxt, datetime(2026, 2, 24, 9, 30))
        secs = market_hours.seconds_until_session(before)
        self.assertAlmostEqual(secs, (nxt - before).total_seconds(), delta=1.0)
        self.assertGreater(secs, 10 * 86400, "旧实现到 10 天就放弃并回落成 60s")

    def test_next_trading_day_helper(self):
        self.assertEqual(market_hours.next_trading_day("2026-09-17"), datetime(2026, 9, 18))
        self.assertEqual(market_hours.next_trading_day("2026-09-18", inclusive=True),
                         datetime(2026, 9, 18))
        self.assertEqual(market_hours.next_trading_day(datetime(2026, 9, 19, 10, 0)),
                         datetime(2026, 9, 21))

    def test_invalid_date_falls_back_to_today(self):
        """脏日期不抛异常 (从今天起算), 免得一个参数把接口打成 500。"""
        with mock.patch.object(market_hours, "now", return_value=datetime(2026, 9, 17, 16, 0)):
            self.assertEqual(market_hours.next_trading_day("not-a-date"), datetime(2026, 9, 18))

    def test_seconds_until_live_counts_auction(self):
        # 08:00 → 距 09:15 还有 4500s (不是距 09:30 的 5400s)
        self.assertAlmostEqual(
            market_hours.seconds_until_live(datetime(2026, 9, 14, 8, 0)), 4500.0, delta=1.0)
        self.assertEqual(market_hours.seconds_until_live(datetime(2026, 9, 14, 10, 0)), 0.0)

    def test_seconds_until_session_keeps_continuous_auction_semantics(self):
        """监控线程仍在 09:30 起轮询, 竞价期不触发价格预警。"""
        self.assertAlmostEqual(
            market_hours.seconds_until_session(datetime(2026, 9, 14, 8, 0)), 5400.0, delta=1.0)
        self.assertAlmostEqual(
            market_hours.seconds_until_session(datetime(2026, 9, 14, 9, 20)), 600.0, delta=1.0)


class MarketStatusTest(unittest.TestCase):
    """接口透传的统一快照: 字段口径与 server_ms 时区。"""

    def _status(self, when: str):
        dt = datetime.strptime(when, "%Y-%m-%d %H:%M:%S")
        with mock.patch.object(market_hours, "is_trading_day", return_value=True):
            return market_hours.market_status(dt)

    def test_payload_during_auction(self):
        st = self._status("2026-09-17 09:23:11")
        self.assertEqual(st["session_phase"], "auction")
        self.assertTrue(st["is_auction"])
        self.assertTrue(st["quote_live"], "竞价期要拉行情")
        self.assertFalse(st["in_session"], "in_session 语义不变: 仍只表示连续竞价")
        self.assertTrue(st["stream_allowed"])
        self.assertEqual(st["next_open_at"], "2026-09-17 09:30:00")
        self.assertEqual(st["next_open_in_sec"], 409.0)
        self.assertIsNone(st["next_live_at"], "已在活跃时段就没有'下一次开始取数'")

    def test_payload_after_close(self):
        st = self._status("2026-09-17 15:30:00")
        self.assertEqual(st["session_phase"], "closed")
        self.assertFalse(st["quote_live"])
        self.assertEqual(st["next_live_at"], "2026-09-18 09:15:00")
        self.assertEqual(st["next_live_in_sec"], 63900.0)
        self.assertEqual(st["calendar_source"], "xshg")

    def test_server_ms_is_beijing_epoch(self):
        """now 是 naive 北京时间: 直接 .timestamp() 会在 UTC 容器里差 8 小时。"""
        st = self._status("2026-09-17 09:23:11")
        expected = int(datetime(2026, 9, 17, 1, 23, 11,
                                tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(st["server_ms"], expected)

    def test_calendar_source_reports_degradation(self):
        with mock.patch.object(market_hours, "_xshg_days",
                               side_effect=ImportError("no pandas_market_calendars")):
            self.assertEqual(market_hours.calendar_source(), "weekday")
        self.assertEqual(market_hours.calendar_source(), "xshg")

    def test_weekday_fallback_still_finds_next_day(self):
        """日历包缺失时退化为周一~周五, 但"下一个交易日"不能变成 None。"""
        with mock.patch.object(market_hours, "_xshg_days",
                               side_effect=ImportError("no calendar")):
            with mock.patch.object(market_hours, "_xshg_sorted",
                                   side_effect=ImportError("no calendar")):
                nxt = market_hours.next_trading_day(datetime(2026, 9, 17))
        self.assertEqual(nxt, datetime(2026, 9, 18))


if __name__ == "__main__":
    unittest.main()
