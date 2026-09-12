# -*- coding: utf-8 -*-
"""/api/drawings 画线同步接口测试 (登录 + CSRF + upsert 往返)。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

SAMPLE_DRAWINGS = [
    {
        "id": "dtest1", "type": "trend",
        "points": [{"t": "2026-01-05", "p": 10.5, "off": -3},
                   {"t": "2026-01-08", "p": 11.2, "off": 0}],
        "style": {"color": "#e6a23c", "width": 1, "dash": False},
        "extendRight": True, "createdAt": 1700000000000,
    },
    {
        "id": "dtest2", "type": "hline",
        "points": [{"t": "2026-01-06", "p": 12.0, "off": 0}],
        "style": {"color": "#409eff", "width": 2, "dash": True},
        "extendRight": False, "createdAt": 1700000000001,
    },
]


class DrawingsApiTest(unittest.TestCase):
    SYMBOL = "600000.SH"
    PERIOD = "1d"

    @classmethod
    def setUpClass(cls):
        import trades
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_drawings.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        if not any(u["username"] == "draw_admin" for u in trades.list_users()):
            trades.create_user("draw_admin", "password123", is_admin=True)
        from app import create_app
        cls.app = create_app()
        cls.client = cls.app.test_client()
        headers = cls._csrf(cls.client)
        r = cls.client.post(
            "/api/auth/login",
            data=json.dumps({"username": "draw_admin", "password": "password123"}),
            content_type="application/json", headers=headers,
        )
        assert r.status_code == 200, r.data

    @classmethod
    def tearDownClass(cls):
        import trades
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    @staticmethod
    def _csrf(client):
        client.get("/login.html")
        headers = {}
        c = client.get_cookie("csrf_token")
        if c is not None:
            headers["X-CSRF-Token"] = c.value if hasattr(c, "value") else str(c)
        return headers

    def test_roundtrip(self):
        # 初始为空
        r = self.client.get(f"/api/drawings?symbol={self.SYMBOL}&period={self.PERIOD}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["drawings"], [])

        # PUT 保存
        r = self.client.put("/api/drawings", data=json.dumps({
            "symbol": self.SYMBOL, "period": self.PERIOD, "drawings": SAMPLE_DRAWINGS,
        }), content_type="application/json", headers=self._csrf(self.client))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["count"], 2)

        # GET 回读一致
        r = self.client.get(f"/api/drawings?symbol={self.SYMBOL}&period={self.PERIOD}")
        data = r.get_json()
        self.assertEqual(len(data["drawings"]), 2)
        self.assertEqual(data["drawings"][0]["id"], "dtest1")
        self.assertEqual(data["drawings"][0]["points"][1]["p"], 11.2)

        # PUT 再次保存 → 整体替换 (upsert)
        r = self.client.put("/api/drawings", data=json.dumps({
            "symbol": self.SYMBOL, "period": self.PERIOD, "drawings": SAMPLE_DRAWINGS[:1],
        }), content_type="application/json", headers=self._csrf(self.client))
        self.assertEqual(r.get_json()["count"], 1)
        r = self.client.get(f"/api/drawings?symbol={self.SYMBOL}&period={self.PERIOD}")
        self.assertEqual(len(r.get_json()["drawings"]), 1)

        # DELETE 清空
        r = self.client.delete(
            f"/api/drawings?symbol={self.SYMBOL}&period={self.PERIOD}",
            headers=self._csrf(self.client))
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/api/drawings?symbol={self.SYMBOL}&period={self.PERIOD}")
        self.assertEqual(r.get_json()["drawings"], [])

    def test_period_isolated(self):
        self.client.put("/api/drawings", data=json.dumps({
            "symbol": self.SYMBOL, "period": "1w", "drawings": SAMPLE_DRAWINGS,
        }), content_type="application/json", headers=self._csrf(self.client))
        r = self.client.get(f"/api/drawings?symbol={self.SYMBOL}&period=1w")
        self.assertEqual(len(r.get_json()["drawings"]), 2)
        r = self.client.get(f"/api/drawings?symbol={self.SYMBOL}&period=1d")
        self.assertEqual(r.get_json()["drawings"], [])
        self.client.delete(f"/api/drawings?symbol={self.SYMBOL}&period=1w",
                           headers=self._csrf(self.client))

    def test_validation(self):
        # drawings 非对象数组
        r = self.client.put("/api/drawings", data=json.dumps({
            "symbol": self.SYMBOL, "period": "1d", "drawings": ["x"],
        }), content_type="application/json", headers=self._csrf(self.client))
        self.assertEqual(r.status_code, 400)
        # 缺参数
        self.assertEqual(self.client.get("/api/drawings?symbol=x").status_code, 400)
        self.assertEqual(
            self.client.put("/api/drawings", data=json.dumps({"drawings": []}),
                            content_type="application/json",
                            headers=self._csrf(self.client)).status_code, 400)
        # 无效 JSON
        r = self.client.put("/api/drawings", data="not json",
                            content_type="application/json",
                            headers=self._csrf(self.client))
        self.assertEqual(r.status_code, 400)

    def test_unauthorized_401(self):
        # app 层 before_request: 未登录 GET /api/* 返回 401
        anon = self.app.test_client()
        r = anon.get(f"/api/drawings?symbol={self.SYMBOL}&period=1d")
        self.assertEqual(r.status_code, 401)

    def test_csrf_required(self):
        # 变更方法缺 CSRF 头 → 403
        client = self.app.test_client()
        client.post("/api/auth/login",
                    data=json.dumps({"username": "draw_admin", "password": "password123"}),
                    content_type="application/json")
        r = client.put("/api/drawings", data=json.dumps({
            "symbol": self.SYMBOL, "period": "1d", "drawings": [],
        }), content_type="application/json")
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
