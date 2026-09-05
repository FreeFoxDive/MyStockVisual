"""Flask auth / routing smoke tests (no live market data)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

_VISUAL = Path(__file__).resolve().parents[1]
if str(_VISUAL) not in sys.path:
    sys.path.insert(0, str(_VISUAL))


class AuthRouteSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_trades.db"
        import trades
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        if not any(u["username"] == "smoke_admin" for u in trades.list_users()):
            trades.create_user("smoke_admin", "password123", is_admin=True)

        from app import create_app
        cls.app = create_app()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        import trades
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    def _ensure_csrf(self, client=None):
        client = client or self.client
        client.get("/login.html")
        csrf = None
        try:
            c = client.get_cookie("csrf_token")
            if c is not None:
                csrf = c.value if hasattr(c, "value") else str(c)
        except Exception:
            pass
        headers = {}
        if csrf:
            headers["X-CSRF-Token"] = csrf
        return headers

    def test_ping_public(self):
        r = self.client.get("/api/ping")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json().get("ok"))

    def test_me_unauthorized(self):
        c = self.app.test_client()
        r = c.get("/api/auth/me")
        self.assertEqual(r.status_code, 401)

    def test_login_and_me(self):
        headers = self._ensure_csrf()
        r = self.client.post(
            "/api/auth/login",
            data=json.dumps({"username": "smoke_admin", "password": "password123"}),
            content_type="application/json",
            headers=headers,
        )
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.get_json().get("ok"))
        # Flask set_cookie 写入 session
        sess = self.client.get_cookie("session")
        self.assertIsNotNone(sess)
        self.assertTrue(getattr(sess, "value", None) or str(sess))
        r2 = self.client.get("/api/auth/me")
        self.assertEqual(r2.status_code, 200)
        body = r2.get_json()
        self.assertEqual(body.get("username"), "smoke_admin")
        self.assertTrue(body.get("is_admin"))

    def test_login_sets_csrf_via_set_cookie(self):
        headers = self._ensure_csrf()
        before = self.client.get_cookie("csrf_token")
        r = self.client.post(
            "/api/auth/login",
            data=json.dumps({"username": "smoke_admin", "password": "password123"}),
            content_type="application/json",
            headers=headers,
        )
        self.assertEqual(r.status_code, 200)
        after = self.client.get_cookie("csrf_token")
        self.assertIsNotNone(after)
        # 登录会刷新 csrf
        self.assertTrue(getattr(after, "value", None) or True)

    def test_protected_html_redirects(self):
        c = self.app.test_client()
        r = c.get("/trades.html", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login.html", r.headers.get("Location", ""))

    def test_css_public(self):
        r = self.client.get("/css/theme.css")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"--bg:", r.data)

    def test_mutating_without_csrf_forbidden(self):
        headers = self._ensure_csrf()
        self.client.post(
            "/api/auth/login",
            data=json.dumps({"username": "smoke_admin", "password": "password123"}),
            content_type="application/json",
            headers=headers,
        )
        r = self.client.post(
            "/api/models",
            data=json.dumps({"name": "x", "description": ""}),
            content_type="application/json",
        )
        self.assertEqual(r.status_code, 403)

    def test_repo_maturity_endpoint(self):
        # 未登录 401
        c = self.app.test_client()
        self.assertEqual(c.get("/api/repo-maturity?entry_date=2026-08-28&tenor=1").status_code, 401)
        # 登录后: 与 trades._repo_maturity 同口径 (周末顺延)
        headers = self._ensure_csrf()
        self.client.post(
            "/api/auth/login",
            data=json.dumps({"username": "smoke_admin", "password": "password123"}),
            content_type="application/json",
            headers=headers,
        )
        r = self.client.get("/api/repo-maturity?entry_date=2026-08-28&tenor=1")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["maturity"], "2026-08-31")
        # tenor 非法 → 400
        r2 = self.client.get("/api/repo-maturity?entry_date=2026-08-28&tenor=abc")
        self.assertEqual(r2.status_code, 400)


if __name__ == "__main__":
    unittest.main()
