# -*- coding: utf-8 -*-
"""GET /api/stock-info 路由测试: 鉴权 / 缺参 / 字段 / 上游异常脱敏。

运行:
    venv/Scripts/python.exe -u visual/test/test_stock_info_api.py
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import security  # noqa: E402
import trades  # noqa: E402


def _reset_rate_limit():
    """测试间共享全局令牌桶, 整套跑时会被其它用例耗尽 → 每个用例前重置。"""
    security._rate_limit_tokens = float(security.RATE_LIMIT_PER_MIN)
    security._rate_limit_last_refill = time.time()


def _csrf(client):
    client.get("/login.html")
    headers = {}
    c = client.get_cookie("csrf_token")
    if c is not None:
        headers["X-CSRF-Token"] = c.value if hasattr(c, "value") else str(c)
    return headers


class StockInfoRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_si.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        trades.create_user("si_u", "password123")
        cls.uid = trades.list_users()[0]["id"]
        from app import create_app
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    def setUp(self):
        _reset_rate_limit()
        self.client = self.app.test_client()
        token, _ = trades.create_session(self.uid)
        self.client.set_cookie("session", token)
        _csrf(self.client)

    def test_requires_login(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get("/api/stock-info?symbol=000001.SZ").status_code, 401)

    def test_missing_symbol(self):
        self.assertEqual(self.client.get("/api/stock-info").status_code, 400)

    def test_ok(self):
        payload = {"symbol": "000001.SZ", "industry": "银行", "pe": 4.43, "trade_status": "trading"}
        with mock.patch("api.market.fetch_stock_info", return_value=payload) as f:
            data = self.client.get("/api/stock-info?symbol=000001.SZ").get_json()
        self.assertEqual(data["industry"], "银行")
        self.assertEqual(data["pe"], 4.43)
        f.assert_called_once()

    def test_upstream_error_sanitized(self):
        with mock.patch("api.market.fetch_stock_info",
                        side_effect=RuntimeError("licence 66D8-9F96 dead")):
            resp = self.client.get("/api/stock-info?symbol=000001.SZ")
        self.assertEqual(resp.status_code, 500)
        err = resp.get_json()["error"]
        self.assertIn("获取基本信息失败", err)
        self.assertNotIn("66D8", err, "上游异常细节不应外泄")


if __name__ == "__main__":
    unittest.main(verbosity=2)
