# -*- coding: utf-8 -*-
""" /api/health 测试: loopback 明细 / 非 loopback 最小载荷 / 令牌桶豁免 /
DB 探活短缓存 / DB 失败降级 / last_error 脱敏 / 异常时 503。

运行:
    venv/Scripts/python.exe -m unittest visual/test/test_health_api.py -v
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import market_hours  # noqa: E402
import security  # noqa: E402
import trades  # noqa: E402


def _status(**kw):
    base = {"running": True, "backend": "rest", "last_poll": None, "last_poll_ts": time.time(),
            "n_symbols": 0, "last_error": None, "in_backoff": False}
    base.update(kw)
    return base


class HealthApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_health.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        from app import create_app
        cls.app = create_app()
        import api.market as api_market
        cls.api_market = api_market

    @classmethod
    def tearDownClass(cls):
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    def setUp(self):
        self.api_market._health_db_cache.update(ts=0.0, ok=True)
        for p in (
            mock.patch.object(market_hours, "in_session", return_value=False),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _client(self, remote=None):
        return self.app.test_client()

    def test_loopback_returns_full_checks(self):
        with mock.patch("monitor.get_status", return_value=_status()):
            r = self._client().get("/api/health")
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertIn("checks", body)
        self.assertIn("monitor", body["checks"])
        self.assertIn("watchdog", body["checks"])
        self.assertIn("db", body["checks"])
        self.assertTrue(body["checks"]["db"]["ok"])

    def test_non_loopback_minimal_payload(self):
        with mock.patch("monitor.get_status", return_value=_status(last_error="token=abcd1234")):
            r = self.app.test_client().get("/api/health", environ_base={"REMOTE_ADDR": "8.8.8.8"})
        body = r.get_json()
        self.assertIn("ok", body)
        self.assertNotIn("checks", body)
        self.assertNotIn("abcd1234", r.get_data(as_text=True))
        self.assertNotIn("backend", r.get_data(as_text=True))

    def test_anonymous_can_access(self):
        r = self.app.test_client().get("/api/health")
        self.assertIn(r.status_code, (200, 503))
        self.assertIn("ok", r.get_json())

    def test_rate_limit_exempt(self):
        with mock.patch.object(security, "_rate_limit_tokens", 0.0), \
             mock.patch.object(security, "_rate_limit_last_refill", time.time()), \
             mock.patch("monitor.get_status", return_value=_status()):
            health = self.app.test_client().get("/api/health")
            ping = self.app.test_client().get("/api/ping")
        self.assertEqual(health.status_code, 200, "health 应豁免令牌桶")
        self.assertEqual(ping.status_code, 429, "普通接口仍受限流")

    def test_db_check_is_cached(self):
        spy = mock.Mock(return_value=1)
        with mock.patch.object(trades, "count_admins", spy), \
             mock.patch("monitor.get_status", return_value=_status()):
            self.app.test_client().get("/api/health")
            self.app.test_client().get("/api/health")
        self.assertEqual(spy.call_count, 1, "TTL 内 DB 检查应只执行一次")
        # 缓存过期后重新探测
        self.api_market._health_db_cache["ts"] = 0.0
        with mock.patch.object(trades, "count_admins", spy), \
             mock.patch("monitor.get_status", return_value=_status()):
            self.app.test_client().get("/api/health")
        self.assertEqual(spy.call_count, 2)

    def test_db_failure_yields_503_without_raising(self):
        with mock.patch.object(trades, "count_admins", side_effect=RuntimeError("db locked")), \
             mock.patch("monitor.get_status", return_value=_status()):
            r = self.app.test_client().get("/api/health")
        self.assertEqual(r.status_code, 503)
        self.assertFalse(r.get_json()["ok"])

    def test_monitor_not_running_is_unhealthy(self):
        with mock.patch("monitor.get_status", return_value=_status(running=False)):
            r = self.app.test_client().get("/api/health")
        self.assertEqual(r.status_code, 503)

    def test_last_error_is_redacted_in_loopback_details(self):
        st = _status(last_error="upstream token=abcd1234 failed")
        with mock.patch("monitor.get_status", return_value=st):
            r = self.app.test_client().get("/api/health")
        self.assertNotIn("abcd1234", r.get_data(as_text=True))
        self.assertIsNotNone(r.get_json()["checks"]["monitor"]["last_error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
