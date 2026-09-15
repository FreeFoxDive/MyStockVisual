# -*- coding: utf-8 -*-
"""/api/trades 录入接口: 校验失败回传固定文案, 不回传异常原文 (CodeQL #47)。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))


def _fake_bar(symbol, date_str):
    return {
        "date": str(date_str)[:10],
        "open": 10.0,
        "high": 99999.0,
        "low": 0.01,
        "close": 10.0,
        "volume": 1_000_000,
    }


class TradesApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import trades
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_trades_api.db"
        cls._orig_db = trades._db_path
        cls._orig_iter = trades.PBKDF2_ITERATIONS
        trades.PBKDF2_ITERATIONS = 1000
        trades.init_db(cls._db)
        if not any(u["username"] == "trade_admin" for u in trades.list_users()):
            trades.create_user("trade_admin", "password123", is_admin=True)
        from app import create_app
        cls.app = create_app()
        cls.client = cls.app.test_client()
        r = cls.client.post(
            "/api/auth/login",
            data=json.dumps({"username": "trade_admin", "password": "password123"}),
            content_type="application/json", headers=cls._csrf(cls.client),
        )
        assert r.status_code == 200, r.data

    @classmethod
    def tearDownClass(cls):
        import trades
        trades._db_path = cls._orig_db
        trades.PBKDF2_ITERATIONS = cls._orig_iter
        cls._tmpdir.cleanup()

    def setUp(self):
        self._bar_patch = mock.patch("market.get_daily_bar", side_effect=_fake_bar)
        self._bar_patch.start()
        self.addCleanup(self._bar_patch.stop)

    @staticmethod
    def _csrf(client):
        client.get("/login.html")
        headers = {}
        c = client.get_cookie("csrf_token")
        if c is not None:
            headers["X-CSRF-Token"] = c.value if hasattr(c, "value") else str(c)
        return headers

    def _create(self, **overrides):
        body = {
            "symbol": "600000.SH",
            "name": "浦发银行",
            "status": "open",
            "entry_price": 10.0,
            "quantity": 100,
            "entry_date": "2026-08-20",
            "entry_reason": "突破买入",
        }
        body.update(overrides)
        return self.client.post(
            "/api/trades", data=json.dumps(body),
            content_type="application/json", headers=self._csrf(self.client),
        )

    def _assert_no_exception_leak(self, r):
        raw = r.get_data(as_text=True)
        for needle in ("Traceback", "ValueError", "Exception", "line "):
            self.assertNotIn(needle, raw)

    def test_create_invalid_price_returns_specific_message(self):
        r = self._create(entry_price=0)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "买入价必须大于 0")
        self._assert_no_exception_leak(r)

    def test_create_missing_symbol_returns_specific_message(self):
        r = self._create(symbol="")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "缺少股票代码")
        self._assert_no_exception_leak(r)

    def test_update_invalid_price_returns_specific_message(self):
        r = self._create()
        self.assertEqual(r.status_code, 201, r.data)
        tid = r.get_json()["trade"]["id"]
        r = self.client.put(
            f"/api/trades/{tid}", data=json.dumps({"entry_price": 0}),
            content_type="application/json", headers=self._csrf(self.client),
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "买入价必须大于 0")
        self._assert_no_exception_leak(r)

    def test_update_invalid_risk_returns_specific_message(self):
        r = self._create()
        tid = r.get_json()["trade"]["id"]
        r = self.client.put(
            f"/api/trades/{tid}", data=json.dumps({"take_profit": -1}),
            content_type="application/json", headers=self._csrf(self.client),
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["error"], "止盈价必须大于 0")
        self._assert_no_exception_leak(r)

    def test_update_missing_trade_404(self):
        r = self.client.put(
            "/api/trades/999999", data=json.dumps({"entry_price": 0}),
            content_type="application/json", headers=self._csrf(self.client),
        )
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["error"], "记录不存在")


if __name__ == "__main__":
    unittest.main()
