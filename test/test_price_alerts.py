# -*- coding: utf-8 -*-
"""价格预警 (任意条件预警) 测试: 规则校验 / CRUD 与用户隔离 / 监控条件触发。

覆盖:
- trades._clean_price_alert_rule 白名单与数值校验
- /api/alerts CRUD + 用户隔离
- monitor._evaluate_price_alerts: AND 组合 / 缺失跳过 / 30 分钟冷却 / 落库 / 推送

运行:
    venv/Scripts/python.exe -u visual/test/test_price_alerts.py
"""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import trades  # noqa: E402


def _csrf(client):
    client.get("/login.html")
    headers = {}
    c = client.get_cookie("csrf_token")
    if c is not None:
        headers["X-CSRF-Token"] = c.value if hasattr(c, "value") else str(c)
    return headers


class CleanRuleTest(unittest.TestCase):
    def test_valid_rule_normalized(self):
        out = trades._clean_price_alert_rule([{"metric": "price", "op": ">=", "value": "10.5"}])
        self.assertEqual(out, [{"metric": "price", "op": ">=", "value": 10.5}])

    def test_rejects_bad_shapes(self):
        for bad in (None, "x", {}, [], [{"metric": "price"} for _ in range(4)]):
            with self.assertRaises(ValueError):
                trades._clean_price_alert_rule(bad)

    def test_rejects_bad_fields(self):
        cases = [
            {"metric": "pe", "op": ">=", "value": 1},       # 指标不在白名单
            {"metric": "price", "op": ">", "value": 1},     # 操作符不在白名单
            {"metric": "price", "op": ">=", "value": "abc"},  # 非数值
            {"metric": "price", "op": ">="},                # 缺 value
            "not-a-dict",
        ]
        for c in cases:
            with self.assertRaises(ValueError):
                trades._clean_price_alert_rule([c])


class AlertRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_alerts.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        trades.create_user("alice_a", "password123")
        trades.create_user("bob_a", "password123")
        users = {u["username"]: u["id"] for u in trades.list_users()}
        cls.alice, cls.bob = users["alice_a"], users["bob_a"]
        from app import create_app
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    def _client(self, uid):
        c = self.app.test_client()
        token, _ = trades.create_session(uid)
        c.set_cookie("session", token)
        _csrf(c)
        return c

    def _post(self, c, body):
        return c.post("/api/alerts", data=json.dumps(body),
                      content_type="application/json", headers=_csrf(c))

    def test_requires_login(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get("/api/alerts").status_code, 401)
        self.assertEqual(anon.post("/api/alerts", data=json.dumps({}),
                                   content_type="application/json").status_code, 401)

    def test_create_validation(self):
        c = self._client(self.alice)
        self.assertEqual(self._post(c, {"rule": [{"metric": "price", "op": ">=", "value": 1}]}).status_code, 400)
        self.assertEqual(self._post(c, {"symbol": "600000.SH"}).status_code, 400)
        r = self._post(c, {"symbol": "600000.SH",
                           "rule": [{"metric": "pe", "op": ">=", "value": 1}]})
        self.assertEqual(r.status_code, 400)

    def test_crud_and_isolation(self):
        ac, bc = self._client(self.alice), self._client(self.bob)
        r = self._post(ac, {"symbol": "600519.sh", "name": "贵州茅台", "note": "n",
                            "rule": [{"metric": "price", "op": ">=", "value": 2000}]})
        self.assertEqual(r.status_code, 201)
        aid = r.get_json()["id"]           # 代码应被归一为大写
        self.assertEqual(trades.list_price_alerts(self.alice)[0]["symbol"], "600519.SH")
        self.assertEqual(bc.get("/api/alerts").get_json()["alerts"], [], "看不到别人的预警")

        # 更新 note / enabled
        r = ac.put(f"/api/alerts/{aid}", data=json.dumps({"note": "改", "enabled": False}),
                   content_type="application/json", headers=_csrf(ac))
        self.assertTrue(r.get_json()["ok"])
        row = trades.list_price_alerts(self.alice)[0]
        self.assertEqual(row["note"], "改")
        self.assertEqual(row["enabled"], 0)

        # 别人更新/删除 → 不生效
        bob_up = bc.put(f"/api/alerts/{aid}", data=json.dumps({"note": "hack"}),
                        content_type="application/json", headers=_csrf(bc))
        self.assertFalse(bob_up.get_json()["ok"])
        bob_del = bc.delete(f"/api/alerts/{aid}", headers=_csrf(bc))
        self.assertFalse(bob_del.get_json()["ok"])
        self.assertEqual(len(trades.list_price_alerts(self.alice)), 1, "bob 不应删掉 alice 的")

        # 本人删除成功
        self.assertTrue(ac.delete(f"/api/alerts/{aid}", headers=_csrf(ac)).get_json()["ok"])
        self.assertEqual(trades.list_price_alerts(self.alice), [])


class MonitorEvalTest(unittest.TestCase):
    """_evaluate_price_alerts: 行情组合判定 / 冷却 / 落库 / 推送。"""

    SYM = "600519.SH"

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_alert_eval.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        trades.create_user("eval_u", "password123")
        cls.uid = trades.list_users()[0]["id"]
        import monitor
        cls.monitor = monitor

    @classmethod
    def tearDownClass(cls):
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    def _alert(self, rule, last_fired_at=None, note=None):
        aid = trades.create_price_alert(self.uid, self.SYM, "贵州茅台", rule, note=note)
        return {"id": aid, "user_id": self.uid, "symbol": self.SYM, "name": "贵州茅台",
                "rule": rule, "note": note, "last_fired_at": last_fired_at}

    def _patch_notify(self):
        return (mock.patch("monitor.dingtalk.send_markdown"),
                mock.patch("monitor.ntfy.send_markdown"))

    def test_and_combination(self):
        rule = [{"metric": "price", "op": ">=", "value": 100},
                {"metric": "change_pct", "op": "<=", "value": -2}]
        now = datetime(2026, 9, 13, 10, 0, 0)
        dt, nt = self._patch_notify()
        with dt as d, nt as n:
            hit = self.monitor._evaluate_price_alerts(
                [self._alert(rule)], {self.SYM: {"last_price": 101.0, "change_pct": -3.0}}, now)
            miss = self.monitor._evaluate_price_alerts(
                [self._alert(rule)], {self.SYM: {"last_price": 101.0, "change_pct": -1.0}}, now)
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["price"], 101.0)
        self.assertEqual(miss, [])
        d.assert_called_once()
        n.assert_called_once()
        self.assertIn("价格预警", d.call_args.args[0])

    def test_missing_quote_skipped(self):
        rule = [{"metric": "price", "op": ">=", "value": 1}]
        now = datetime(2026, 9, 13, 10, 0, 0)
        dt, nt = self._patch_notify()
        with dt as d, nt as n:
            self.assertEqual(self.monitor._evaluate_price_alerts([self._alert(rule)], {}, now), [])
            self.assertEqual(self.monitor._evaluate_price_alerts(
                [self._alert(rule)], {self.SYM: {"last_price": None}}, now), [])
            self.assertEqual(self.monitor._evaluate_price_alerts(
                [self._alert(rule)], {self.SYM: {"change_pct": 5.0}}, now), [])
        d.assert_not_called()
        n.assert_not_called()

    def test_cooldown_30min(self):
        rule = [{"metric": "price", "op": ">=", "value": 1}]
        now = datetime(2026, 9, 13, 10, 0, 0)
        quotes = {self.SYM: {"last_price": 10.0}}
        dt, nt = self._patch_notify()
        with dt, nt:
            inside = self._alert(rule, last_fired_at=(now - timedelta(seconds=1799)).isoformat())
            outside = self._alert(rule, last_fired_at=(now - timedelta(seconds=1801)).isoformat())
            self.assertEqual(self.monitor._evaluate_price_alerts([inside], quotes, now), [])
            self.assertEqual(len(self.monitor._evaluate_price_alerts([outside], quotes, now)), 1)

    def test_persist_marks_fired_and_records_alert(self):
        rule = [{"metric": "price", "op": ">=", "value": 1}]
        now = datetime(2026, 9, 13, 10, 0, 0)
        a = self._alert(rule, note="备注")
        dt, nt = self._patch_notify()
        with dt, nt:
            fired = self.monitor._evaluate_price_alerts([a], {self.SYM: {"last_price": 12.5}}, now)
        self.assertEqual(len(fired), 1)
        row = next(r for r in trades.list_price_alerts(self.uid) if r["id"] == a["id"])
        self.assertIsNotNone(row["last_fired_at"], "触发后应记冷却时间")
        alerts = trades.list_monitor_alerts(self.uid)
        self.assertEqual(alerts[0]["alert_type"], "price_alert")
        self.assertEqual(alerts[0]["symbol"], self.SYM)
        self.assertEqual(alerts[0]["price"], 12.5)
        self.assertEqual(alerts[0]["detail"], "备注")

    def test_no_persist_no_notify(self):
        rule = [{"metric": "price", "op": ">=", "value": 1}]
        now = datetime(2026, 9, 13, 10, 0, 0)
        a = self._alert(rule)
        before = len(trades.list_monitor_alerts(self.uid))
        dt, nt = self._patch_notify()
        with dt as d, nt as n:
            fired = self.monitor._evaluate_price_alerts(
                [a], {self.SYM: {"last_price": 12.5}}, now, persist=False, notify=False)
        self.assertEqual(len(fired), 1)
        self.assertEqual(len(trades.list_monitor_alerts(self.uid)), before)
        row = next(r for r in trades.list_price_alerts(self.uid) if r["id"] == a["id"])
        self.assertIsNone(row["last_fired_at"])
        d.assert_not_called()
        n.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
