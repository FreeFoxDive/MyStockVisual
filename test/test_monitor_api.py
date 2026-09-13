# -*- coding: utf-8 -*-
"""监控页 API 测试: overview 合并形状 / 用户隔离 / 自服务监控开关。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import trades  # noqa: E402  (需先注入 visual/ 到 sys.path)

# 录入校验会用真实行情源校验交易日K; 测试环境无源, 统一放行
# (high/low 拉满, 使任意录入价都落在当日振幅内)
_PERMISSIVE_BAR = {"open": 10, "high": 1e9, "low": 0.01, "close": 10, "volume": 1000}


def _permissive_bar(symbol, date_str):
    return dict(_PERMISSIVE_BAR, date=str(date_str)[:10])


def _csrf(client):
    client.get("/login.html")
    headers = {}
    c = client.get_cookie("csrf_token")
    if c is not None:
        headers["X-CSRF-Token"] = c.value if hasattr(c, "value") else str(c)
    return headers


class MonitorApiTest(unittest.TestCase):
    # 各测试用独立 symbol, 避免共享 DB 的跨用例污染
    ALICE_SYM = "600519.SH"
    BOB_SYM = "000858.SZ"
    HOLD_SYM = "601318.SH"

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_monitor_page.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        cls._bar_patch = mock.patch("market.get_daily_bar", side_effect=_permissive_bar)
        cls._bar_patch.start()
        # admin + 两个普通用户
        trades.create_user("admin1", "password123", is_admin=True)
        trades.create_user("alice", "password123", is_admin=False)
        trades.create_user("bob", "password123", is_admin=False)
        users = {u["username"]: u["id"] for u in trades.list_users()}
        cls.admin_id, cls.alice_id, cls.bob_id = users["admin1"], users["alice"], users["bob"]
        cls.uid = users
        # alice 开启监控; bob 关闭
        trades.set_user_monitor(cls.alice_id, True)
        trades.set_user_monitor(cls.bob_id, False)
        # alice 一笔带风控价的持仓
        trades.create_trade(cls.alice_id, {
            "symbol": cls.ALICE_SYM, "name": "贵州茅台", "status": "open",
            "entry_price": 100.0, "quantity": 100, "entry_date": "2026-01-05",
            "entry_reason": "测试",
            "take_profit": 120.0, "stop_loss": 90.0, "breakeven": 105.0,
        })
        # bob 一笔带风控价的持仓 (bob 未开监控, 不应出现在任何监控清单)
        trades.create_trade(cls.bob_id, {
            "symbol": cls.BOB_SYM, "name": "五粮液", "status": "open",
            "entry_price": 10.0, "quantity": 1000, "entry_date": "2026-01-05",
            "entry_reason": "测试",
            "take_profit": 12.0, "stop_loss": 9.0, "breakeven": 10.5,
        })
        from app import create_app
        cls.app = create_app()
        cls.clients = {}

    @classmethod
    def tearDownClass(cls):
        cls._bar_patch.stop()
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    def _client(self, username):
        """直接注入会话 Cookie (绕开 /api/auth/login —— 全量套件下登录限流 5/min/IP 会被 429)。"""
        if username not in self.clients:
            client = self.app.test_client()
            token, _ = trades.create_session(self.uid[username])
            client.set_cookie("session", token)
            _csrf(client)  # 预置 CSRF cookie
            self.clients[username] = client
        return self.clients[username]

    def test_overview_requires_login(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get("/api/monitor/overview").status_code, 401)

    def test_overview_scoped_to_self(self):
        c = self._client("alice")
        r = c.get("/api/monitor/overview")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertTrue(data["monitor_enabled"])
        symbols = [p["symbol"] for p in data["positions"]]
        self.assertIn(self.ALICE_SYM, symbols)
        self.assertNotIn(self.BOB_SYM, symbols, "非管理员只能看到自己的持仓")
        row = next(p for p in data["positions"] if p["symbol"] == self.ALICE_SYM)
        self.assertTrue(row["has_risk_prices"])
        self.assertEqual(row["take_profit"], 120.0)
        self.assertEqual(row["stop_loss"], 90.0)
        self.assertEqual(row["breakeven"], 105.0)
        self.assertIn("status", data)
        self.assertIn("alerts", data)
        self.assertIsInstance(data["alerts"], list)

    def test_overview_admin_sees_all_and_scoping(self):
        c = self._client("admin1")
        data = c.get("/api/monitor/overview").get_json()
        self.assertTrue(data["is_admin"])
        self.assertEqual(data["positions"], [], "管理员默认只看自己 (无持仓)")
        data2 = c.get("/api/monitor/overview?all=1").get_json()
        self.assertTrue(data2["scope_all"])
        symbols = [p["symbol"] for p in data2["positions"]]
        self.assertIn(self.ALICE_SYM, symbols)
        self.assertNotIn(self.BOB_SYM, symbols, "bob 未开启监控, 不纳入")
        row = next(p for p in data2["positions"] if p["symbol"] == self.ALICE_SYM)
        self.assertEqual(row["username"], "alice")

    def test_overview_disabled_user_excluded(self):
        c = self._client("bob")
        symbols = [p["symbol"] for p in c.get("/api/monitor/overview").get_json()["positions"]]
        self.assertNotIn(self.BOB_SYM, symbols, "bob 未开启监控")
        trades.set_user_monitor(self.bob_id, True)
        try:
            symbols2 = [p["symbol"] for p in
                        c.get("/api/monitor/overview").get_json()["positions"]]
            self.assertIn(self.BOB_SYM, symbols2)
        finally:
            trades.set_user_monitor(self.bob_id, False)

    def test_hold_expire_merge(self):
        # alice 关联模型 (hold_days=10) 的持仓 → 合并出 hold 字段与到期日
        mid = trades.create_model("监控页测试模型", "", 10)
        try:
            trades.create_trade(self.alice_id, {
                "symbol": self.HOLD_SYM, "name": "中国平安", "status": "open",
                "entry_price": 11.0, "quantity": 500, "entry_date": "2026-01-05",
                "entry_reason": "测试", "model_id": mid,
            })
            c = self._client("alice")
            data = c.get("/api/monitor/overview").get_json()
            row = next(p for p in data["positions"] if p["symbol"] == self.HOLD_SYM)
            self.assertTrue(row["has_hold_expire"])
            self.assertFalse(row["has_risk_prices"], "未填风控价, 只挂到期提醒")
            self.assertEqual(row["model_name"], "监控页测试模型")
            self.assertEqual(row["hold_days"], 10)
            self.assertIsNotNone(row["hold_end_date"], "应预计算到期日")
            self.assertIsInstance(row["hold_days_left"], int)
        finally:
            trades.delete_model(mid)

    def test_self_toggle_enabled(self):
        c = self._client("bob")
        r = c.post("/api/monitor/enabled", data=json.dumps({"enabled": True}),
                   content_type="application/json", headers=_csrf(c))
        self.assertTrue(r.get_json()["ok"])
        row = next(u for u in trades.list_users() if u["id"] == self.bob_id)
        self.assertTrue(row["monitor_enabled"])
        r2 = c.post("/api/monitor/enabled", data=json.dumps({"enabled": False}),
                    content_type="application/json", headers=_csrf(c))
        self.assertTrue(r2.get_json()["ok"])
        row = next(u for u in trades.list_users() if u["id"] == self.bob_id)
        self.assertFalse(row["monitor_enabled"])

    def test_admin_toggle_rejected(self):
        c = self._client("admin1")
        r = c.post("/api/monitor/enabled", data=json.dumps({"enabled": False}),
                   content_type="application/json", headers=_csrf(c))
        body = r.get_json()
        self.assertFalse(body["ok"])
        self.assertTrue(body["is_admin"])
        self.assertTrue(body["monitor_enabled"], "管理员恒开")

    def test_toggle_requires_login_and_validation(self):
        anon = self.app.test_client()
        self.assertEqual(anon.post("/api/monitor/enabled",
                                   data=json.dumps({"enabled": True}),
                                   content_type="application/json").status_code, 401)
        c = self._client("alice")
        r = c.post("/api/monitor/enabled", data=json.dumps({"enabled": "yes"}),
                   content_type="application/json", headers=_csrf(c))
        self.assertEqual(r.status_code, 400)

    # ── 价格监控 (趋势线跌破 + 条件预警) 在监控页的暴露 ──
    TL_SYM = "600036.SH"
    PA_SYM = "601988.SH"

    @staticmethod
    def _trendline_drawing(did="dtl1"):
        return {
            "id": did, "type": "trend", "name": "上升支撑线",
            "points": [{"t": "2026-01-05", "p": 12.0, "off": 0},
                       {"t": "2026-02-05", "p": 14.0, "off": 0}],
            "style": {"color": "#e6a23c", "width": 1, "dash": False},
            "monitor": {"enabled": True, "pct": 2.0, "adjust": "forward"},
        }

    def test_overview_includes_price_monitoring(self):
        c = self._client("alice")
        trades.save_chart_drawings(self.alice_id, self.TL_SYM, "1d",
                                   [self._trendline_drawing()])
        aid = trades.create_price_alert(
            self.alice_id, self.PA_SYM, "低价提醒",
            [{"metric": "price", "op": "<=", "value": 9.5}], note="观察")
        try:
            data = c.get("/api/monitor/overview").get_json()
            tls = {m["drawing_id"]: m for m in data["trendline_monitors"]}
            self.assertIn("dtl1", tls)
            self.assertEqual(tls["dtl1"]["symbol"], self.TL_SYM)
            self.assertEqual(tls["dtl1"]["pct"], 2.0)
            self.assertEqual(tls["dtl1"]["adjust"], "forward")
            pas = {a["id"]: a for a in data["price_alerts"]}
            self.assertIn(aid, pas)
            self.assertEqual(pas[aid]["rule"],
                             [{"metric": "price", "op": "<=", "value": 9.5}])
            self.assertTrue(pas[aid]["enabled"])
            # 用户隔离: bob 看不到 alice 的价格监控配置
            bob = self._client("bob").get("/api/monitor/overview").get_json()
            self.assertEqual(bob["trendline_monitors"], [])
            self.assertEqual(bob["price_alerts"], [])
        finally:
            trades.delete_chart_drawings(self.alice_id, self.TL_SYM, "1d")
            trades.delete_price_alert(self.alice_id, aid)

    def test_trendline_disable_endpoint(self):
        c = self._client("alice")
        trades.save_chart_drawings(self.alice_id, self.TL_SYM, "1d",
                                   [self._trendline_drawing("dtl2")])
        try:
            anon = self.app.test_client()
            self.assertEqual(anon.put(
                "/api/trendline-monitors/dtl2",
                data=json.dumps({"enabled": False}),
                content_type="application/json").status_code, 401)

            r = c.put("/api/trendline-monitors/dtl2",
                      data=json.dumps({"enabled": "no"}),
                      content_type="application/json", headers=_csrf(c))
            self.assertEqual(r.status_code, 400)

            bob = self._client("bob")
            r = bob.put("/api/trendline-monitors/dtl2",
                        data=json.dumps({"enabled": False}),
                        content_type="application/json", headers=_csrf(bob))
            self.assertFalse(r.get_json()["ok"], "不能停用他人监控线")
            self.assertIn("dtl2", [m["drawing_id"] for m in
                                   trades.list_trendline_monitors(self.alice_id)])

            r = c.put("/api/trendline-monitors/dtl2",
                      data=json.dumps({"enabled": False}),
                      content_type="application/json", headers=_csrf(c))
            body = r.get_json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["symbol"], self.TL_SYM)
            self.assertEqual(body["period"], "1d")
            self.assertEqual([m["drawing_id"] for m in
                              trades.list_trendline_monitors(self.alice_id)], [])
            saved = trades.get_chart_drawings(self.alice_id, self.TL_SYM, "1d")
            self.assertEqual(saved[0]["name"], "上升支撑线", "画线本身保留")
            self.assertEqual(saved[0]["monitor"]["pct"], 2.0)
            self.assertFalse(saved[0]["monitor"]["enabled"])
        finally:
            trades.delete_chart_drawings(self.alice_id, self.TL_SYM, "1d")


if __name__ == "__main__":
    unittest.main()
