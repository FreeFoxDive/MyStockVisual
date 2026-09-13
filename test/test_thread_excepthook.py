# -*- coding: utf-8 -*-
"""threading.excepthook: 线程未捕获异常记录到日志并由观察者异步告警; 钩子本身健壮。

运行:
    venv/Scripts/python.exe -m unittest visual/test/test_thread_excepthook.py -v
"""
from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import error_notify  # noqa: E402
import server  # noqa: E402


class _Recorder:
    def __init__(self):
        self.calls = []

    def dingtalk(self, title, text):
        self.calls.append(("dingtalk", title, text))
        return True

    def ntfy(self, title, text):
        self.calls.append(("ntfy", title, text))
        return True


class ThreadExcepthookTest(unittest.TestCase):
    def setUp(self):
        self.rec = _Recorder()
        self._orig_hook = threading.excepthook
        self._orig_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.INFO)
        error_notify.install_log_handler()
        self.addCleanup(lambda: setattr(threading, "excepthook", self._orig_hook))
        self.addCleanup(lambda: logging.getLogger().setLevel(self._orig_level))
        self.addCleanup(error_notify.uninstall_log_handler)
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

    def test_thread_exception_is_logged_and_notified(self):
        server._install_thread_excepthook()

        def boom():
            raise RuntimeError("thread-boom")

        t = threading.Thread(target=boom, name="boom-thread")
        t.start()
        t.join(timeout=5)

        self.assertEqual(error_notify.drain_once(), 1)
        text = self.rec.calls[0][2]
        self.assertIn("RuntimeError", text)
        self.assertIn("test_thread_excepthook.py", text)

    def test_hook_is_robust_to_missing_fields(self):
        server._install_thread_excepthook()
        # 直接调用钩子: thread / exc_value 为 None 也不得抛异常
        threading.excepthook(SimpleNamespace(thread=None, exc_value=None,
                                             exc_type=RuntimeError, exc_traceback=None))
        threading.excepthook(SimpleNamespace(thread=None, exc_value=None,
                                             exc_type=None, exc_traceback=None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
