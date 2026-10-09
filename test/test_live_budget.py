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

    def test_wait_uses_same_interval_without_burst(self):
        budget = PacedBudget(48)
        clock = [100.0]
        with patch('live_budget.time.monotonic', side_effect=lambda: clock[0]), \
             patch('live_budget.time.sleep', side_effect=lambda sec: clock.__setitem__(0, clock[0] + sec)) as sleep:
            self.assertTrue(budget.try_acquire())
            self.assertTrue(budget.acquire(timeout=2.0))
            sleep.assert_called_once_with(1.25)
            self.assertEqual(list(budget.starts), [100.0, 101.25])
            self.assertFalse(budget.try_acquire())

    def test_wait_timeout_does_not_spend_token(self):
        budget = PacedBudget(48)
        clock = [100.0]
        with patch('live_budget.time.monotonic', side_effect=lambda: clock[0]), \
             patch('live_budget.time.sleep', side_effect=lambda sec: clock.__setitem__(0, clock[0] + sec)) as sleep:
            self.assertTrue(budget.try_acquire())
            self.assertFalse(budget.acquire(timeout=0.5))
            sleep.assert_called_once_with(0.5)
            self.assertEqual(list(budget.starts), [100.0])

    def test_wait_respects_full_rolling_window(self):
        budget = PacedBudget(2)
        clock = [10.0]
        with patch('live_budget.time.monotonic', side_effect=lambda: clock[0]), \
             patch('live_budget.time.sleep', side_effect=lambda sec: clock.__setitem__(0, clock[0] + sec)) as sleep:
            budget.starts.extend([10.0, 20.0])
            budget.next_at = 30.0
            clock[0] = 40.0  # pacing interval passed, rolling window still full
            self.assertTrue(budget.acquire(timeout=31.0))
            sleep.assert_called_once_with(30.0)
            self.assertEqual(list(budget.starts), [20.0, 70.0])

    def test_delayed_wakeup_after_deadline_does_not_spend_token(self):
        budget = PacedBudget(48)
        clock = [100.0]
        with patch('live_budget.time.monotonic', side_effect=lambda: clock[0]), \
             patch('live_budget.time.sleep', side_effect=lambda sec: clock.__setitem__(0, 101.5)) as sleep:
            self.assertTrue(budget.try_acquire())
            self.assertFalse(budget.acquire(timeout=0.5))
            sleep.assert_called_once_with(0.5)
            self.assertEqual(list(budget.starts), [100.0])
            self.assertEqual(budget.next_at, 101.25)

    def test_token_ready_at_deadline_is_not_consumed(self):
        budget = PacedBudget(48)
        clock = [100.0]
        with patch('live_budget.time.monotonic', side_effect=lambda: clock[0]), \
             patch('live_budget.time.sleep', side_effect=lambda sec: clock.__setitem__(0, clock[0] + sec)) as sleep:
            self.assertTrue(budget.try_acquire())
            self.assertFalse(budget.acquire(timeout=1.25))
            sleep.assert_called_once_with(1.25)
            self.assertEqual(list(budget.starts), [100.0])

    def test_zero_timeout_keeps_initial_nonblocking_attempt(self):
        budget = PacedBudget(48)
        with patch('live_budget.time.monotonic', return_value=100.0), \
             patch('live_budget.time.sleep') as sleep:
            self.assertTrue(budget.acquire(timeout=0.0))
            self.assertFalse(budget.acquire(timeout=0.0))
            sleep.assert_not_called()
            self.assertEqual(list(budget.starts), [100.0])
