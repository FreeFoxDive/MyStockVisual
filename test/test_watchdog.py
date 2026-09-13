# -*- coding: utf-8 -*-
"""watchdog 测试: 死线程自愈(退避/熔断) / 卡死与误报排除 / 持续失败 / 进程级退出 /
自保包装 / 状态更新。

运行:
    venv/Scripts/python.exe -m unittest visual/test/test_watchdog.py -v
"""
from __future__ import annotations

import os
import queue
import sys
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import error_notify  # noqa: E402
import monitor  # noqa: E402
import watchdog  # noqa: E402


def _status(**kw):
    base = {
        "running": True, "backend": "rest", "last_poll": None, "last_poll_ts": None,
        "n_symbols": 0, "last_error": None, "in_backoff": False,
    }
    base.update(kw)
    return base


class _Recorder:
    def __init__(self):
        self.calls = []

    def dingtalk(self, title, text):
        self.calls.append(("dingtalk", title, text))
        return True

    def ntfy(self, title, text):
        self.calls.append(("ntfy", title, text))
        return True


class WatchdogTest(unittest.TestCase):
    def setUp(self):
        self.rec = _Recorder()
        watchdog.reset_state()
        for p in (
            mock.patch.object(error_notify, "_q", queue.Queue(maxsize=error_notify.QUEUE_MAX)),
            mock.patch.object(error_notify, "_recent", {}),
            mock.patch.object(error_notify, "_sent_times", deque()),
            mock.patch.object(error_notify, "_dropped", 0),
            mock.patch.object(error_notify, "_suppressed", 0),
            mock.patch.object(error_notify, "_start_worker", lambda: None),
            mock.patch.dict(os.environ, {"ERROR_NOTIFY_DISABLED": "", "WATCHDOG_EXIT_ON_STALL": ""}),
        ):
            p.start()
            self.addCleanup(p.stop)
        import dingtalk
        import ntfy
        for p in (
            mock.patch.object(dingtalk, "send_markdown", self.rec.dingtalk),
            mock.patch.object(ntfy, "send_markdown", self.rec.ntfy),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(watchdog.reset_state)

    def _patches(self, alive, status, start_background=None):
        ps = [
            mock.patch.object(monitor, "is_thread_alive", return_value=alive),
            mock.patch.object(monitor, "get_status", return_value=status),
        ]
        if start_background is not None:
            ps.append(mock.patch.object(monitor, "start_background", start_background))
        return ps

    # ── 死线程自愈 ──
    def test_dead_thread_restarts_and_notifies(self):
        sb = mock.Mock(return_value=object())
        with mock.patch.object(monitor, "is_thread_alive", return_value=False), \
             mock.patch.object(monitor, "get_status", return_value=_status()), \
             mock.patch.object(monitor, "start_background", sb):
            actions = watchdog.check_once(now=1000, in_session=False)
        self.assertIn("thread-dead", actions)
        self.assertIn("restart:ok", actions)
        sb.assert_called_once()
        self.assertGreaterEqual(error_notify.drain_once(), 1)

    def test_restart_backoff_defers_second_attempt(self):
        sb = mock.Mock(return_value=object())
        with mock.patch.object(monitor, "is_thread_alive", return_value=False), \
             mock.patch.object(monitor, "get_status", return_value=_status()), \
             mock.patch.object(monitor, "start_background", sb):
            watchdog.check_once(now=1000, in_session=False)
            actions2 = watchdog.check_once(now=1010, in_session=False)
        self.assertIn("restart:backoff", actions2)
        self.assertEqual(sb.call_count, 1)

    def test_breaker_stops_restarting_after_max(self):
        sb = mock.Mock(side_effect=RuntimeError("cannot start"))
        with mock.patch.object(watchdog, "MAX_RESTARTS", 2), \
             mock.patch.object(monitor, "is_thread_alive", return_value=False), \
             mock.patch.object(monitor, "get_status", return_value=_status()), \
             mock.patch.object(monitor, "start_background", sb):
            watchdog.check_once(now=1000, in_session=False)
            watchdog.check_once(now=2000, in_session=False)
            actions3 = watchdog.check_once(now=4000, in_session=False)
        self.assertIn("restart:breaker", actions3)
        self.assertEqual(sb.call_count, 2, "熔断后不得再尝试重启")
        self.assertTrue(watchdog.get_state()["breaker"])

    # ── 卡死 / 误报 ──
    def test_stall_notifies_when_heartbeat_stale(self):
        now = 100000.0
        watchdog._session_seen_ts = now - watchdog.GRACE_SEC - 1  # 已过宽限
        st = _status(last_poll_ts=now - watchdog.STALL_SEC - 5)
        with mock.patch.object(monitor, "is_thread_alive", return_value=True), \
             mock.patch.object(monitor, "get_status", return_value=st):
            actions = watchdog.check_once(now=now, in_session=True)
        self.assertIn("stall", actions)
        self.assertGreaterEqual(error_notify.drain_once(), 1)
        self.assertIn("Stall", self.rec.calls[0][2])

    def test_no_stall_when_in_backoff(self):
        now = 100000.0
        watchdog._session_seen_ts = now - watchdog.GRACE_SEC - 1
        st = _status(last_poll_ts=now - watchdog.STALL_SEC - 5, in_backoff=True)
        with mock.patch.object(monitor, "is_thread_alive", return_value=True), \
             mock.patch.object(monitor, "get_status", return_value=st):
            actions = watchdog.check_once(now=now, in_session=True)
        self.assertNotIn("stall", actions)

    def test_no_stall_within_grace_period(self):
        now = 100000.0
        st = _status(last_poll_ts=now - watchdog.STALL_SEC - 5)
        watchdog.reset_state()  # _session_seen_ts = None → 首见即宽限起点
        with mock.patch.object(monitor, "is_thread_alive", return_value=True), \
             mock.patch.object(monitor, "get_status", return_value=st):
            actions = watchdog.check_once(now=now, in_session=True)
        self.assertNotIn("stall", actions)

    def test_no_stall_when_last_poll_missing(self):
        now = 100000.0
        watchdog._session_seen_ts = now - watchdog.GRACE_SEC - 1
        st = _status(last_poll_ts=None)
        with mock.patch.object(monitor, "is_thread_alive", return_value=True), \
             mock.patch.object(monitor, "get_status", return_value=st):
            actions = watchdog.check_once(now=now, in_session=True)
        self.assertNotIn("stall", actions)

    def test_exit_on_stall_calls_os_exit(self):
        now = 100000.0
        watchdog._session_seen_ts = now - watchdog.GRACE_SEC - 1
        st = _status(last_poll_ts=now - watchdog.STALL_SEC - 5)
        with mock.patch.dict(os.environ, {"WATCHDOG_EXIT_ON_STALL": "1"}), \
             mock.patch.object(monitor, "is_thread_alive", return_value=True), \
             mock.patch.object(monitor, "get_status", return_value=st), \
             mock.patch.object(watchdog.os, "_exit") as ex:
            watchdog.check_once(now=now, in_session=True)
        ex.assert_called_once_with(1)

    def test_no_exit_on_stall_by_default(self):
        now = 100000.0
        watchdog._session_seen_ts = now - watchdog.GRACE_SEC - 1
        st = _status(last_poll_ts=now - watchdog.STALL_SEC - 5)
        with mock.patch.object(monitor, "is_thread_alive", return_value=True), \
             mock.patch.object(monitor, "get_status", return_value=st), \
             mock.patch.object(watchdog.os, "_exit") as ex:
            watchdog.check_once(now=now, in_session=True)
        ex.assert_not_called()

    # ── 持续失败 ──
    def test_persistent_failure_notifies_once(self):
        now = 100000.0
        watchdog._session_seen_ts = now - watchdog.GRACE_SEC - 1
        st = _status(last_error="upstream down", last_poll_ts=now)
        with mock.patch.object(monitor, "is_thread_alive", return_value=True), \
             mock.patch.object(monitor, "get_status", return_value=st):
            a1 = watchdog.check_once(now=now, in_session=True)
            a2 = watchdog.check_once(now=now + watchdog.FAIL_SEC + 1, in_session=True)
            a3 = watchdog.check_once(now=now + watchdog.FAIL_SEC + 2, in_session=True)
        self.assertNotIn("persistent-failure", a1)
        self.assertIn("persistent-failure", a2)
        self.assertNotIn("persistent-failure", a3, "同一错误不应重复告警")

    def test_failure_state_resets_when_recovered(self):
        now = 100000.0
        watchdog._session_seen_ts = now - watchdog.GRACE_SEC - 1
        with mock.patch.object(monitor, "is_thread_alive", return_value=True), \
             mock.patch.object(monitor, "get_status", return_value=_status(last_error="x")):
            watchdog.check_once(now=now, in_session=True)
        with mock.patch.object(monitor, "is_thread_alive", return_value=True), \
             mock.patch.object(monitor, "get_status", return_value=_status(last_error=None)):
            watchdog.check_once(now=now + 1, in_session=True)
        self.assertIsNone(watchdog._fail_key)

    # ── 自保 / 状态 ──
    def test_run_once_safe_swallows_exceptions(self):
        with mock.patch.object(monitor, "get_status", side_effect=RuntimeError("status blew up")):
            actions = watchdog.run_once_safe(now=1, in_session=False)
        self.assertEqual(actions, ["error"])

    def test_state_records_last_check(self):
        with mock.patch.object(monitor, "is_thread_alive", return_value=True), \
             mock.patch.object(monitor, "get_status", return_value=_status()):
            watchdog.check_once(now=1234, in_session=False)
        self.assertEqual(watchdog.get_state()["last_check_ts"], 1234)


if __name__ == "__main__":
    unittest.main(verbosity=2)
