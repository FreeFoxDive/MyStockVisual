import unittest
from unittest.mock import patch

from live_budget import PacedBudget


class LiveBudgetTest(unittest.TestCase):
    def test_no_burst_and_rolling_limit(self):
        budget = PacedBudget(48)
        with patch('live_budget.time.monotonic') as clock:
            for i in range(48):
                clock.return_value = i * 1.25
                self.assertTrue(budget.try_acquire())
                self.assertFalse(budget.try_acquire(), 'same instant must not burst')
            clock.return_value = 59.999
            self.assertFalse(budget.try_acquire())
            clock.return_value = 60
            self.assertTrue(budget.try_acquire())

    def test_latency_does_not_add_another_full_interval(self):
        budget = PacedBudget(24)
        with patch('live_budget.time.monotonic') as clock:
            clock.return_value = 0
            self.assertTrue(budget.try_acquire())
            clock.return_value = 0.7
            self.assertFalse(budget.try_acquire())
            clock.return_value = 2.5
            self.assertTrue(budget.try_acquire())
            clock.return_value = 100
            self.assertTrue(budget.try_acquire())
            self.assertFalse(budget.try_acquire(), 'idle time never accumulates a burst')
