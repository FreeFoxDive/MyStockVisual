# -*- coding: utf-8 -*-
"""/api/me/panel-config 面板设置账号同步接口测试 (登录 + CSRF + 校验往返)。"""

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

SAMPLE_CONFIG = {
    "volume": True, "macd": True, "kdj": False, "chip": True,
    "info": True, "depth": True, "cross": True, "patterns": True,
    "impulse": False, "channel": True, "gap": True, "logScale": False,
    "adjust": "hfq", "maPeriods": [5, 13, 34], "bollParams": {"n": 20, "k": 2.5},
    "periodMore": False, "indMore": True,
}


class PanelConfigApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import trades
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_panel_config.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        if not any(u["username"] == "panel_admin" for u in trades.list_users()):
            trades.create_user("panel_admin", "password123", is_admin=True)
        from app import create_app
        cls.app = create_app()
        cls.client = cls.app.test_client()
        # 整套跑时全局登录 IP 令牌桶会被其它用例耗尽 → 登录前重置 (与 test_mr_data_api 同理)
        security._login_attempts.clear()
        r = cls.client.post(
            "/api/auth/login",
            data=json.dumps({"username": "panel_admin", "password": "password123"}),
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

    def _put(self, config):
        return self.client.put(
            "/api/me/panel-config",
            data=json.dumps({"config": config}),
            content_type="application/json", headers=self._csrf(self.client),
        )

    def test_roundtrip(self):
        # 初始为空
        r = self.client.get("/api/me/panel-config")
        self.assertEqual(r.status_code, 200)

        r = self._put(SAMPLE_CONFIG)
        self.assertEqual(r.status_code, 200)
        cfg = r.get_json()["config"]
        self.assertTrue(cfg["cross"], "金叉死叉默认开的配置应原样保存")
        self.assertEqual(cfg["adjust"], "hfq")
        self.assertEqual(cfg["maPeriods"], [5, 13, 34])
        self.assertEqual(cfg["bollParams"], {"n": 20, "k": 2.5})
        self.assertIs(cfg["indMore"], True)

        r = self.client.get("/api/me/panel-config")
        self.assertEqual(r.get_json()["config"], cfg)

    def test_theme_not_synced(self):
        # 主题走本地共享键, 服务端不落库 (即使客户端误传也丢弃)
        r = self._put({"volume": True, "theme": "dark", "themeAuto": False})
        cfg = r.get_json()["config"]
        self.assertNotIn("theme", cfg)
        self.assertNotIn("themeAuto", cfg)

    def test_normalize_filters_bad_values(self):
        r = self._put({
            "volume": "yes",           # 布尔键: 强制转 bool
            "unknownKey": 1,           # 未知键丢弃
            "adjust": "sideways",      # 非法复权丢弃
            "maPeriods": [5, 10],      # 长度不对丢弃
            "bollParams": {"n": 1, "k": -1},  # 非法参数丢弃
            "__proto__": {"x": 1},
        })
        cfg = r.get_json()["config"]
        self.assertEqual(cfg, {"volume": True})

    def test_risk_key_in_whitelist(self):
        # 主页持仓风控线开关跟账号同步: 白名单缺键会被静默丢弃
        import trades
        self.assertIn("risk", trades.PANEL_CONFIG_BOOL_KEYS)
        r = self._put({"risk": False})
        self.assertEqual(r.get_json()["config"], {"risk": False})
        self.assertEqual(self.client.get("/api/me/panel-config").get_json()["config"],
                         {"risk": False})

    def test_empty_does_not_clear(self):
        self._put(SAMPLE_CONFIG)
        before = self.client.get("/api/me/panel-config").get_json()["config"]
        r = self._put({})
        self.assertEqual(r.get_json()["config"], before, "空配置不得清空已有设置")
        self.assertEqual(self.client.get("/api/me/panel-config").get_json()["config"], before)

    def test_invalid_json_400(self):
        r = self.client.put("/api/me/panel-config", data="not json",
                            content_type="application/json", headers=self._csrf(self.client))
        self.assertEqual(r.status_code, 400)

    def test_unauthorized_401(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get("/api/me/panel-config").status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
