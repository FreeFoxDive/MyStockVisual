# -*- coding: utf-8 -*-
"""market_hours.session_phase 边界测试 (唯一时段口径)。

运行:
    venv/Scripts/python.exe -u visual/test/test_market_hours.py
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import market_hours  # noqa: E402


class SessionPhaseTest(unittest.TestCase):
    def _phase(self, when: str, trading=True):
        dt = datetime.strptime(when, "%Y-%m-%d %H:%M")
        with mock.patch.object(market_hours, "is_trading_day", return_value=trading):
            return market_hours.session_phase(dt)

    def test_non_trading_day(self):
        self.assertEqual(self._phase("2026-09-13 10:00", trading=False), "non_trading")

    def test_pre_open(self):
        self.assertEqual(self._phase("2026-09-14 09:29"), "pre")
        self.assertEqual(self._phase("2026-09-14 00:05"), "pre")

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

    def test_bar_ready_phases_exclude_pre(self):
        self.assertIn("trading", market_hours.BAR_READY_PHASES)
        self.assertIn("break", market_hours.BAR_READY_PHASES)
        self.assertIn("closed", market_hours.BAR_READY_PHASES)
        self.assertNotIn("pre", market_hours.BAR_READY_PHASES)
        self.assertNotIn("non_trading", market_hours.BAR_READY_PHASES)


if __name__ == "__main__":
    unittest.main()
