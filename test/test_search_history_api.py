# -*- coding: utf-8 -*-
"""/api/me/search-history 单条删除接口测试 (登录 + CSRF + 与 PUT 清空语义的差异)。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import security  # noqa: E402

ENTRIES = [
    {"symbol": "000001.SZ", "name": "平安银行", "ts": 300},
    {"symbol": "600000.SH", "name": "浦发银行", "ts": 200},
]


class SearchHistoryApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import trades
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_search_history.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        if not any(u["username"] == "hist_admin" for u in trades.list_users()):
            trades.create_user("hist_admin", "password123", is_admin=True)
        from app import create_app
        cls.app = create_app()
        cls.client = cls.app.test_client()
        # 整套跑时全局登录 IP 令牌桶会被其它用例耗尽 → 登录前重置 (同 test_panel_config_api)
        security._login_attempts.clear()
        r = cls.client.post(
            "/api/auth/login",
            data=json.dumps({"username": "hist_admin", "password": "password123"}),
            content_type="application/json", headers=cls._csrf(cls.client),
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

    def setUp(self):
        # 每条用例都从固定两条历史开始
        self._put(ENTRIES)

    def _put(self, history):
        return self.client.put(
            "/api/me/search-history",
            data=json.dumps({"history": history}),
            content_type="application/json", headers=self._csrf(self.client),
        )

    def _delete(self, symbol, *, client=None, with_csrf=True):
        c = client or self.client
        url = "/api/me/search-history"
        if symbol is not None:
            url += "?symbol=" + symbol
        headers = self._csrf(c) if with_csrf else {}
        return c.delete(url, headers=headers)

    def _symbols(self):
        r = self.client.get("/api/me/search-history")
        self.assertEqual(r.status_code, 200)
        return [h["symbol"] for h in r.get_json()["history"]]

    def test_delete_one_and_persist(self):
        r = self._delete("600000.SH")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([h["symbol"] for h in r.get_json()["history"]], ["000001.SZ"])
        self.assertEqual(self._symbols(), ["000001.SZ"], "GET 复核已落库")

    def test_delete_may_empty_but_put_empty_does_not(self):
        """DELETE 能删到空; PUT [] 仍被服务端 guard 拒绝 (两条路径语义不同)。"""
        self.assertEqual(self._delete("000001.SZ").status_code, 200)
        self.assertEqual(self._delete("600000.SH").get_json()["history"], [])
        self.assertEqual(self._symbols(), [])

        self._put(ENTRIES)
        self._put([])
        self.assertEqual(self._symbols(), ["000001.SZ", "600000.SH"],
                         "PUT [] 不得抹掉账号历史")

    def test_delete_unknown_symbol_keeps_all(self):
        r = self._delete("999999.SZ")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._symbols(), ["000001.SZ", "600000.SH"])

    def test_missing_symbol_400(self):
        self.assertEqual(self._delete(None).status_code, 400)
        self.assertEqual(self._delete("").status_code, 400)
        self.assertEqual(self._symbols(), ["000001.SZ", "600000.SH"])

    def test_unauthorized_401(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get("/api/me/search-history").status_code, 401)
        # 鉴权在 CSRF 之前 (app.py: 未登录 401), 故匿名 DELETE 也是 401
        self.assertEqual(self._delete("000001.SZ", client=anon, with_csrf=False).status_code, 401)
        self.assertEqual(self._symbols(), ["000001.SZ", "600000.SH"])

    def test_csrf_required(self):
        r = self._delete("000001.SZ", with_csrf=False)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self._symbols(), ["000001.SZ", "600000.SH"],
                         "CSRF 失败不得改动历史")


if __name__ == "__main__":
    unittest.main(verbosity=2)
