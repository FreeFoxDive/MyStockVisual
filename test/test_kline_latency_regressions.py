"""有界工作、熔断恢复、溢价生命周期与跨日磁盘缓存的行为回归。"""
import io
import json
import sys
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kline_source as ks
import market
import premium


class SourceLifecycle(unittest.TestCase):
    def setUp(self):
        ks.reset_health()
        self.release = threading.Event()
        self.workers = []

    def tearDown(self):
        self.release.set()
        for worker in self.workers:
            worker.join(2)
        ks.reset_health()

    def hung(self, *args):
        self.workers.append(threading.current_thread())
        self.release.wait(2)
        return None

    def test_timeouts_retain_slots_until_actual_exit(self):
        src = Mock(fetch=self.hung)
        with patch.object(ks, 'SOURCE_TIMEOUT_SEC', .01), \
             patch.object(ks, 'SOURCE_MAX_INFLIGHT', 2):
            for _ in range(2):
                self.assertIsNone(ks._fetch_bounded(src, 'x', '1d', 10, 'none', 'hung-test'))
            for _ in range(5):
                with self.assertRaises(ks.SourceBusy):
                    ks._fetch_bounded(src, 'x', '1d', 10, 'none', 'hung-test')
            self.assertEqual(ks._active['hung-test'], 2)
            self.release.set()
            for worker in self.workers:
                worker.join(2)
            self.assertNotIn('hung-test', ks._active)

    def test_global_capacity_does_not_count_as_source_failure(self):
        with patch.object(ks, 'SOURCE_MAX_TOTAL', 1), \
             patch.object(ks, 'SOURCE_TIMEOUT_SEC', .01), \
             patch.dict(ks.SOURCES, {'busy-test': Mock(fetch=self.hung)}):
            ks._fetch_bounded(Mock(fetch=self.hung), 'x', '1d', 10, 'none', 'global-test')
            self.assertIsNone(ks._try_source('busy-test', 'x', '1d', 10, 'none', 'stock', 0, ['busy-test']))
            self.assertEqual(ks._health['busy-test']['fails'], 0)

    def test_cooldown_expiry_admits_only_one_probe(self):
        with patch.object(ks, 'SOURCE_FAIL_THRESHOLD', 1):
            ks._note_fail('probe-test')
            self.assertIsNone(ks._admit('probe-test'))
            ks._health['probe-test']['until'] = time.monotonic() - 1
            token = ks._admit('probe-test')
            self.assertIsNotNone(token)
            self.assertIsNone(ks._admit('probe-test'))
            ks._note_fail('probe-test', token)
            self.assertTrue(ks._in_cooldown('probe-test'))
            ks._health['probe-test']['until'] = time.monotonic() - 1
            token = ks._admit('probe-test')
            ks._note_ok('probe-test', token)
            self.assertFalse(ks._in_cooldown('probe-test'))

    def test_old_success_cannot_clear_new_cooldown(self):
        old = ks._admit('late-test')
        with patch.object(ks, 'SOURCE_FAIL_THRESHOLD', 1):
            ks._note_fail('late-test', old)
        ks._note_ok('late-test', old)
        self.assertTrue(ks._in_cooldown('late-test'))


class PremiumLifecycle(unittest.TestCase):
    def setUp(self):
        premium.clear()
        self.release = threading.Event()
        self.started = threading.Event()
        self.workers = []

    def tearDown(self):
        self.release.set()
        for worker in self.workers:
            worker.join(2)
        premium.clear()

    def hung(self, *args, **kwargs):
        self.workers.append(threading.current_thread())
        self.started.set()
        self.release.wait(2)
        return {'dates': ['2026-09-14'], 'values': [1.0], 'params': {}}

    def test_hung_jobs_stay_bounded_and_report_failure(self):
        with patch.object(premium, 'PREMIUM_MAX_INFLIGHT', 1), \
             patch.object(premium, 'compute_premium', side_effect=self.hung) as compute:
            premium.request('510300.SH', '1d', 1006)
            self.assertTrue(self.started.wait(1))
            key = premium._key('510300.SH', '1d', 1006)
            premium._inflight[key] -= premium.PREMIUM_TIMEOUT_SEC + 1
            result = premium.get('510300.SH', '1d', 1006)
            self.assertEqual(result['status'], 'failed')
            for symbol in ('510050.SH', '159915.SZ', '510300.SH'):
                premium.request(symbol, '1d', 1006)
            self.assertEqual(compute.call_count, 1)
            self.assertEqual(len(premium._inflight), 1)

    def test_expired_payload_returned_stale_while_refresh_pending(self):
        key = premium._key('510300.SH', '1d', 1006)
        premium._cache[key] = (0, {'dates': ['2026-09-14'], 'values': [1.0]})
        with patch.object(premium, 'compute_premium', side_effect=self.hung):
            result = premium.get('510300.SH', '1d', 1006)
            self.assertTrue(result['ready'])
            self.assertTrue(result['stale'])
            self.assertEqual(result['values'], [1.0])
            self.assertEqual(result['status'], 'pending')

    def test_cross_day_and_expiry_cannot_be_fresh(self):
        with patch.object(market.market_hours, 'now', return_value=datetime(2026, 9, 15)):
            self.assertFalse(premium._fresh((time.time(), {'generated_date': '2026-09-14'})))
            self.assertTrue(premium._fresh((time.time(), {'generated_date': '2026-09-15'})))

    def test_negative_cache_defers_retry_then_recovers(self):
        key = premium._key('510300.SH', '1d', 1006)
        premium._fail_at[key] = time.monotonic()
        premium._fail_kind[key] = 'no_data'
        with patch.object(premium, 'compute_premium', side_effect=self.hung) as compute:
            got = premium.get('510300.SH', '1d', 1006)
            self.assertEqual(got['status'], 'no_data')
            self.assertGreater(got['retry_after_sec'], 0)
            compute.assert_not_called()
            premium._fail_at[key] -= premium.PREMIUM_FAIL_TTL + 1
            premium.get('510300.SH', '1d', 1006)
            self.assertTrue(self.started.wait(1))
            self.assertEqual(compute.call_count, 1)

    def test_session_transition_invalidates_long_off_hours_cache(self):
        stamp = datetime(2026, 9, 15, 15, 1)
        ent = (time.time(), {'generated_date': '2026-09-15', 'generated_phase': 'trading'})
        with patch.object(market.market_hours, 'now', return_value=stamp), \
             patch.object(market.market_hours, 'session_phase', return_value='post'):
            self.assertFalse(premium._fresh(ent))

    def test_client_rechecks_off_hours_ttl_within_a_minute(self):
        stamp = datetime(2026, 9, 15, 8)
        key = premium._key('510300.SH', '1d', 1006)
        premium._cache[key] = (time.time(), {'generated_date': '2026-09-15',
            'generated_phase': 'pre', 'dates': ['2026-09-14'], 'values': [1]})
        with patch.object(market.market_hours, 'now', return_value=stamp), \
             patch.object(market.market_hours, 'session_phase', return_value='pre'), \
             patch.object(premium, 'compute_premium') as compute:
            result = premium.get('510300.SH', '1d', 1006)
            self.assertGreater(result['max_age_sec'], 0)
            self.assertLessEqual(result['max_age_sec'], 60)
            compute.assert_not_called()

    def test_nav_singleflight_waiter_timeout_does_not_start_another_fetch(self):
        with market._etf_nav_lock:
            market._etf_nav_cache.clear()
        with patch.object(market, '_fetch_etf_nav_uncached', side_effect=self.hung) as fetch, \
             patch.object(ks, 'SOURCE_TIMEOUT_SEC', .01):
            worker = threading.Thread(target=market._fetch_etf_nav, args=('510300.SH',))
            worker.start()
            self.assertTrue(self.started.wait(1))
            with self.assertRaises(TimeoutError):
                market._fetch_etf_nav('510300.SH')
            self.assertEqual(fetch.call_count, 1)
            self.release.set()
            worker.join(2)


class DiskDateBoundary(unittest.TestCase):
    def test_reject_previous_day_even_inside_ttl(self):
        now = datetime(2026, 9, 15, 0, 1, tzinfo=timezone(timedelta(hours=8)))
        cache = market.DiskCache.__new__(market.DiskCache)
        path = Mock()
        path.stat.return_value.st_mtime = now.timestamp() - 120
        with patch.object(cache, '_key', return_value=path), \
             patch.object(market.time, 'time', return_value=now.timestamp()), \
             patch.object(market.market_hours, 'now', return_value=now):
            # 元数据和旧格式 mtime 两种情况均不可跨日。
            for payload in ({'generated_date': '2026-09-14'}, {'data': []}):
                with patch.object(market.gzip, 'open', return_value=io.StringIO(json.dumps(payload))):
                    self.assertIsNone(cache.get('x', '1d', 10, 1800))
            with patch.object(market.gzip, 'open', return_value=io.StringIO('{"data": []}')):
                self.assertEqual(cache.get('x', '1w', 10, 1800), {'data': []})


if __name__ == '__main__':
    unittest.main()
