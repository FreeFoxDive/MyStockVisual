# -*- coding: utf-8 -*-
"""monitor 可靠性: in_backoff 透出 status; 循环异常 → 异步通知且不阻塞/不退出。

运行:
    venv/Scripts/python.exe -m unittest visual/test/test_monitor_reliability.py -v
"""
from __future__ import annotations

import os
import queue
import sys
import time
import unittest
from collections import deque
from datetime import datetime
from pathlib import Path
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import error_notify  # noqa: E402
import monitor  # noqa: E402


class _Stop(Exception):
    """在 sleep 处打断 _loop 的无限循环。"""


class _Recorder:
    def __init__(self):
        self.calls = []

    def dingtalk(self, title, text):
        self.calls.append(("dingtalk", title, text))
        return True

    def ntfy(self, title, text):
        self.calls.append(("ntfy", title, text))
        return True


class _FakeFeed:
    backend = "fake"

    def in_backoff(self):
        return False

    def poll_interval(self, n):
        return 0.0


class MonitorReliabilityTest(unittest.TestCase):
    def setUp(self):
        self.rec = _Recorder()
        self._orig_status = dict(monitor._status)
        for p in (
            mock.patch.object(error_notify, "_q", queue.Queue(maxsize=error_notify.QUEUE_MAX)),
            mock.patch.object(error_notify, "_recent", {}),
            mock.patch.object(error_notify, "_sent_times", deque()),
            mock.patch.object(error_notify, "_dropped", 0),
            mock.patch.object(error_notify, "_suppressed", 0),
            mock.patch.object(error_notify, "_start_worker", lambda: None),
            mock.patch.dict(os.environ, {"ERROR_NOTIFY_DISABLED": ""}),
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
        self.addCleanup(lambda: monitor._status.update(self._orig_status))

    def test_status_exposes_in_backoff(self):
        self.assertIn("in_backoff", monitor.get_status())
        monitor._set_status(in_backoff=True)
        self.assertTrue(monitor.get_status()["in_backoff"])
        monitor._set_status(in_backoff=False)
        self.assertFalse(monitor.get_status()["in_backoff"])

    def test_loop_notifies_on_exception_without_blocking(self):
        calls = {"sleep": 0}

        def fake_sleep(sec):
            calls["sleep"] += 1
            raise _Stop()

        with mock.patch.object(monitor.feed_mod, "RestFeed", lambda *a, **k: _FakeFeed()), \
             mock.patch.object(monitor.market_hours, "in_session", lambda now: True), \
             mock.patch.object(monitor.market_hours, "now", lambda: datetime(2026, 9, 11, 10, 0, 0)), \
             mock.patch.object(monitor, "_check_hold_expire", lambda **k: None), \
             mock.patch.object(monitor.trades, "list_monitored_positions",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(monitor.time, "sleep", side_effect=fake_sleep):
            t0 = time.perf_counter()
            with self.assertRaises(_Stop):
                monitor._loop(lambda: None, None)
            elapsed = time.perf_counter() - t0

        self.assertLess(elapsed, 2.0, "通知不得阻塞循环(应很快走到 sleep 断点)")
        self.assertEqual(monitor.get_status()["last_error"], "boom")
        self.assertEqual(error_notify.drain_once(), 1)
        self.assertIn("RuntimeError", self.rec.calls[0][2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
