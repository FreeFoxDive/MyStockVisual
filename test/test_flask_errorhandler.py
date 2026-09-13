# -*- coding: utf-8 -*-
"""Flask 错误处理测试: 未预期异常交回框架(原生 HTML 500) + /api/ HTTP 错误 JSON
(保留 Allow 等头) + 不泄漏异常原文 + 日志观察者告警。

运行:
    venv/Scripts/python.exe -m unittest visual/test/test_flask_errorhandler.py -v
"""
from __future__ import annotations

import logging
import os
import queue
import sys
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import error_notify  # noqa: E402
import trades  # noqa: E402


class _Recorder:
    def __init__(self):
        self.calls = []

    def dingtalk(self, title, text):
        self.calls.append(("dingtalk", title, text))
        return True

    def ntfy(self, title, text):
        self.calls.append(("ntfy", title, text))
        return True


class FlaskErrorHandlerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_err.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        trades.create_user("err_u", "password123")
        cls.uid = trades.list_users()[0]["id"]

        import app as app_module
        cls.app_module = app_module
        cls._orig_root_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.INFO)
        cls.app = app_module.create_app()

        def boom():
            raise ZeroDivisionError("secret-token-xyz")

        def only_get():
            from flask import jsonify
            return jsonify({"ok": True})

        def big():
            from flask import request
            request.get_data()
            from flask import jsonify
            return jsonify({"ok": True})

        cls.app.add_url_rule("/api/_boom", "_boom", boom, methods=["GET"])
        cls.app.add_url_rule("/api/_only_get", "_only_get", only_get, methods=["GET"])
        cls.app.add_url_rule("/api/_big", "_big", big, methods=["POST"])
        # _big 需绕过 CSRF 才能读到 body 触发 413
        cls._post_patch = mock.patch.object(
            app_module, "PUBLIC_API_POST",
            set(app_module.PUBLIC_API_POST) | {"/api/_big", "/api/_only_get"},
        )
        cls._post_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._post_patch.stop()
        error_notify.uninstall_log_handler()
        logging.getLogger().setLevel(cls._orig_root_level)
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    def setUp(self):
        self.rec = _Recorder()
        self.client = self.app.test_client()
        token, _ = trades.create_session(self.uid)
        self.client.set_cookie("session", token)
        error_notify.install_log_handler()
        for p in (
            mock.patch.object(error_notify, "_start_worker", lambda: None),
            mock.patch.object(error_notify, "_q", queue.Queue(maxsize=error_notify.QUEUE_MAX)),
            mock.patch.object(error_notify, "_recent", {}),
            mock.patch.object(error_notify, "_sent_times", deque()),
            mock.patch.object(error_notify, "_dropped", 0),
            mock.patch.object(error_notify, "_suppressed", 0),
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

    def test_unhandled_exception_returns_native_html_500_and_notifies(self):
        r = self.client.get("/api/_boom")
        self.assertEqual(r.status_code, 500)
        self.assertEqual(r.mimetype, "text/html", "未预期异常应保持 Flask 原生 HTML 500")
        self.assertNotIn(b"secret-token-xyz", r.data, "响应体不得泄漏异常原文")

        self.assertEqual(error_notify.drain_once(), 1)
        text = self.rec.calls[0][2]
        self.assertIn("ZeroDivisionError", text)
        self.assertIn("test_flask_errorhandler.py", text)

    def test_api_404_is_json(self):
        r = self.client.get("/api/_definitely_missing")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.mimetype, "application/json")
        self.assertIn("error", r.get_json())

    def test_405_keeps_allow_header(self):
        r = self.client.post("/api/_only_get")
        self.assertEqual(r.status_code, 405)
        self.assertEqual(r.mimetype, "application/json")
        self.assertIn("Allow", r.headers, "405 必须保留框架生成的 Allow 头: " + str(dict(r.headers)))

    def test_413_is_json(self):
        r = self.client.post("/api/_big", data=b"x" * 1_100_000)
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.mimetype, "application/json")

    def test_non_api_404_unchanged(self):
        anon = self.app.test_client()
        r = anon.get("/definitely-missing.json")
        self.assertEqual(r.status_code, 302, "非 API 路径不应被 JSON handler 接管")
        self.assertIn("/login.html", r.headers.get("Location", ""))

    def test_401_json_regression(self):
        anon = self.app.test_client()
        r = anon.get("/api/trades")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.mimetype, "application/json")

    def test_429_json_regression(self):
        import security
        with mock.patch.object(security, "_rate_limit_tokens", 0.0), \
             mock.patch.object(security, "_rate_limit_last_refill", time.time()):
            r = self.client.get("/api/_only_get")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.mimetype, "application/json")


if __name__ == "__main__":
    unittest.main(verbosity=2)
