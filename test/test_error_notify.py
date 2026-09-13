# -*- coding: utf-8 -*-
"""error_notify 单元测试: 非阻塞 / 队列丢弃 / 窗口聚合 / 全局预算 / 脱敏 /
通道隔离 / 日志观察者(最深帧定位、畸形 exc_info、防递归、幂等)。

运行:
    venv/Scripts/python.exe -m unittest visual/test/test_error_notify.py -v
"""
from __future__ import annotations

import logging
import os
import queue
import sys
import time
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import dingtalk  # noqa: E402
import ntfy  # noqa: E402
import error_notify  # noqa: E402


class _Recorder:
    """替代真实推送通道: 记录 (channel, title, text)。"""

    def __init__(self):
        self.calls = []
        self.delay = 0.0
        self.raise_on_dingtalk = False
        self.raise_on_ntfy = False

    def dingtalk(self, title, text):
        if self.delay:
            time.sleep(self.delay)
        if self.raise_on_dingtalk:
            raise RuntimeError("dingtalk down")
        self.calls.append(("dingtalk", title, text))
        return True

    def ntfy(self, title, text):
        if self.delay:
            time.sleep(self.delay)
        if self.raise_on_ntfy:
            raise RuntimeError("ntfy down")
        self.calls.append(("ntfy", title, text))
        return True


class ErrorNotifyTest(unittest.TestCase):
    def setUp(self):
        self.rec = _Recorder()
        # 全局补丁: 通道替换 + 禁用真实工作线程(测试用 drain_once 手动驱动, 保证确定性)
        for p in (
            mock.patch.object(error_notify, "_q", queue.Queue(maxsize=error_notify.QUEUE_MAX)),
            mock.patch.object(error_notify, "_recent", {}),
            mock.patch.object(error_notify, "_sent_times", deque()),
            mock.patch.object(error_notify, "_dropped", 0),
            mock.patch.object(error_notify, "_suppressed", 0),
            mock.patch.object(error_notify, "_start_worker", lambda: None),
            mock.patch.object(dingtalk, "send_markdown", self.rec.dingtalk),
            mock.patch.object(ntfy, "send_markdown", self.rec.ntfy),
            mock.patch.dict(os.environ, {"ERROR_NOTIFY_DISABLED": ""}),
        ):
            p.start()
            self.addCleanup(p.stop)

    # ── 非阻塞 / 队列 ──
    def test_caller_never_sends_synchronously(self):
        self.rec.delay = 0.5
        t0 = time.perf_counter()
        for _ in range(3):
            error_notify.notify_error("s", "RuntimeError", "a.py:1")
        elapsed = time.perf_counter() - t0
        self.assertLess(elapsed, 0.05, "notify_error 必须零阻塞")
        self.assertEqual(self.rec.calls, [], "调用方不得同步发送")

    def test_queue_full_drops_and_counts(self):
        with mock.patch.object(error_notify, "_q", queue.Queue(maxsize=2)):
            for i in range(5):
                error_notify.notify_error(f"s{i}", "E", "x:1")
            self.assertEqual(error_notify.stats()["dropped"], 3)
            self.assertEqual(error_notify.stats()["queued"], 2)

    def test_disabled_env_is_noop(self):
        with mock.patch.dict(os.environ, {"ERROR_NOTIFY_DISABLED": "1"}):
            self.assertFalse(error_notify.notify_error("s", "E", "x:1"))
        self.assertEqual(error_notify._q.qsize(), 0)

    # ── 聚合 / 预算 ──
    def test_same_source_aggregates_and_window_resets(self):
        for _ in range(3):
            error_notify.notify_error("app-500", "ZeroDivisionError", "a.py:10")
        self.assertEqual(error_notify.drain_once(), 1)
        self.assertEqual([c[0] for c in self.rec.calls], ["dingtalk", "ntfy"])
        self.assertIn("3 次", self.rec.calls[0][2])

        # 窗口内再次上报 → 抑制, 不再发送
        error_notify.notify_error("app-500", "ZeroDivisionError", "a.py:10")
        self.assertEqual(error_notify.drain_once(), 0)

        # 窗口过期 → 重新发送
        error_notify._recent["app-500"][0] -= error_notify.WINDOW_SEC + 1
        error_notify.notify_error("app-500", "ZeroDivisionError", "a.py:10")
        self.assertEqual(error_notify.drain_once(), 1)

    def test_different_sources_both_sent(self):
        error_notify.notify_error("s1", "E", "a:1")
        error_notify.notify_error("s2", "E", "b:2")
        self.assertEqual(error_notify.drain_once(), 2)
        self.assertEqual(len(self.rec.calls), 4)

    def test_global_budget_caps_sends(self):
        with mock.patch.object(error_notify, "BUDGET_PER_MIN", 2):
            for i in range(5):
                error_notify.notify_error(f"s{i}", "E", "a:1")
                error_notify.drain_once()
            self.assertLessEqual(len(self.rec.calls), 2 * 2)

    def test_source_normalization_merges_thread_names(self):
        a = error_notify.normalize_source("Thread-7 (_scan_worker)")
        b = error_notify.normalize_source("Thread-19 (_scan_worker)")
        self.assertEqual(a, b)

    # ── 脱敏 ──
    def test_summary_is_redacted_and_has_no_traceback(self):
        text = error_notify._summary_text(
            "s", "RuntimeError", "api.py:1 token=abcd1234 password=hunter2", 3
        )
        self.assertNotIn("abcd1234", text)
        self.assertNotIn("hunter2", text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn('File "', text)

    # ── 通道隔离 ──
    def test_channel_failure_isolated(self):
        self.rec.raise_on_dingtalk = True
        error_notify.notify_error("s", "E", "a:1")
        error_notify.drain_once()
        self.assertEqual([c[0] for c in self.rec.calls], ["ntfy"])

    # ── 日志观察者 ──
    def test_observer_uses_deepest_frame_location(self):
        error_notify.install_log_handler()
        self.addCleanup(error_notify.uninstall_log_handler)
        try:
            raise ZeroDivisionError("boom")
        except ZeroDivisionError:
            logging.getLogger("test.observer").error("boom", exc_info=True)
        self.assertEqual(error_notify.drain_once(), 1)
        text = self.rec.calls[0][2]
        self.assertIn("ZeroDivisionError", text)
        self.assertIn("test_error_notify.py", text,
                      "位置应取 exc_info 最深栈帧(本测试文件), 而非 logging 调用点")

    def test_observer_malformed_exc_info_is_safe(self):
        h = error_notify._ObserverHandler()
        rec = logging.LogRecord("x", logging.ERROR, "p", 1, "m", None, None)
        rec.exc_info = None
        h.emit(rec)
        rec2 = logging.LogRecord("x", logging.ERROR, "p", 1, "m", None, None)
        rec2.exc_info = ("OnlyType",)  # 长度不足, 应被安全忽略
        h.emit(rec2)
        self.assertEqual(error_notify._q.qsize(), 0)

    def test_observer_skips_own_loggers(self):
        h = error_notify._ObserverHandler()
        for name in ("error_notify", "dingtalk", "ntfy", "myappnotify.ntfy"):
            rec = logging.LogRecord(name, logging.ERROR, "p", 1, "m", None, None)
            rec.exc_info = (RuntimeError, RuntimeError("x"), None)
            h.emit(rec)
        self.assertEqual(error_notify._q.qsize(), 0)

    def test_no_recursion_when_channel_logs_error(self):
        def bad_channel(title, text):
            logging.getLogger("dingtalk").error("channel blew up", exc_info=True)
            return False

        with mock.patch.object(dingtalk, "send_markdown", bad_channel):
            error_notify.install_log_handler()
            self.addCleanup(error_notify.uninstall_log_handler)
            error_notify.notify_error("s", "E", "a:1")
            self.assertEqual(error_notify.drain_once(), 1)
        # 通道自身 ERROR 日志不得回头产生新告警
        self.assertEqual(error_notify._q.qsize(), 0)

    def test_install_is_idempotent(self):
        h1 = error_notify.install_log_handler()
        h2 = error_notify.install_log_handler()
        self.addCleanup(error_notify.uninstall_log_handler)
        self.assertIs(h1, h2)
        count = sum(
            isinstance(x, error_notify._ObserverHandler)
            for x in logging.getLogger().handlers
        )
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
