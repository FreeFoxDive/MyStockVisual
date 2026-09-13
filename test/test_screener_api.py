# -*- coding: utf-8 -*-
"""条件选股 screener 测试: 纯函数条件判定/特征计算 + 路由与落盘 (不触发全量扫描)。

关键约束: 任何用例都不得让 /api/screener/run 真正启动 `_scan_worker`
(否则会按默认 300 只标的 + 6/min 令牌桶跑几十分钟)。路由用例统一 patch _scan_worker。

运行:
    venv/Scripts/python.exe -u visual/test/test_screener_api.py
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

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


def _closes_df(values):
    return pd.DataFrame({"close": [float(v) for v in values]})


class MatchConditionsTest(unittest.TestCase):
    """_match_conditions: 纯函数, 零 mock。"""

    def setUp(self):
        from api import screener
        self.m = screener._match_conditions

    def test_boolean_metrics(self):
        feats = {"macd_cross_up": True, "above_ma20": False}
        self.assertTrue(self.m(feats, [{"metric": "macd_cross_up"}]))
        self.assertFalse(self.m(feats, [{"metric": "above_ma20"}]))
        # 值为 None 的布尔条件不受影响
        self.assertTrue(self.m(feats, [{"metric": "macd_cross_up", "value": None}]))

    def test_numeric_thresholds(self):
        feats = {"rsi6": 20.0, "change_pct": 5.0, "chip_profit": 80.0}
        self.assertTrue(self.m(feats, [{"metric": "rsi6_lt", "value": 30}]))
        self.assertFalse(self.m(feats, [{"metric": "rsi6_lt", "value": 10}]))
        self.assertTrue(self.m(feats, [{"metric": "change_pct_gt", "value": 3}]))
        self.assertFalse(self.m(feats, [{"metric": "change_pct_gt", "value": 9}]))
        self.assertTrue(self.m(feats, [{"metric": "chip_profit_gt", "value": 50}]))
        self.assertFalse(self.m(feats, [{"metric": "chip_profit_gt", "value": 90}]))

    def test_missing_feature_fails(self):
        self.assertFalse(self.m({}, [{"metric": "rsi6_lt", "value": 30}]))
        self.assertFalse(self.m({"rsi6": None}, [{"metric": "rsi6_lt", "value": 30}]))
        self.assertFalse(self.m({}, [{"metric": "change_pct_gt", "value": 3}]))
        self.assertFalse(self.m({}, [{"metric": "chip_profit_gt", "value": 3}]))
        self.assertFalse(self.m({}, [{"metric": "above_ma20"}]))

    def test_unknown_metric_fails(self):
        self.assertFalse(self.m({"above_ma20": True}, [{"metric": "bogus"}]))

    def test_and_combination(self):
        feats = {"above_ma20": True, "rsi6": 20.0}
        self.assertTrue(self.m(feats, [
            {"metric": "above_ma20"}, {"metric": "rsi6_lt", "value": 30}]))
        self.assertFalse(self.m(feats, [
            {"metric": "above_ma20"}, {"metric": "rsi6_lt", "value": 10}]))

    def test_empty_conditions_pass(self):
        self.assertTrue(self.m({}, []))


class ComputeFeatsTest(unittest.TestCase):
    """_compute_feats: 纯函数, 构造 DataFrame 断言。"""

    def setUp(self):
        from api import screener
        self.f = screener._compute_feats
        self.up = [10.0 + 0.1 * i for i in range(40)]          # 单调上涨
        self.cross = ([20.0 - 0.2 * i for i in range(25)]      # 先跌
                      + [15.0 + 0.5 * i for i in range(15)])   # 后急涨 (DIF 上穿 DEA)

    def test_change_pct_formula(self):
        feats = self.f(_closes_df(self.up), [{"metric": "above_ma20"}])
        expected = (self.up[-1] - self.up[-6]) / self.up[-6] * 100
        self.assertAlmostEqual(feats["change_pct"], expected, places=9)

    def test_change_pct_none_when_short(self):
        feats = self.f(_closes_df([10.0, 10.1, 10.2, 10.3]), [])
        self.assertIsNone(feats["change_pct"])

    def test_above_ma20(self):
        self.assertTrue(self.f(_closes_df(self.up), [])["above_ma20"])
        self.assertFalse(self.f(_closes_df(self.up[::-1]), [])["above_ma20"])

    def test_macd_only_computed_when_requested(self):
        feats_off = self.f(_closes_df(self.cross), [{"metric": "above_ma20"}])
        self.assertNotIn("macd_cross_up", feats_off)
        feats_on = self.f(_closes_df(self.cross), [{"metric": "macd_cross_up"}])
        from indicators import macd
        dif, dea, _h = macd(pd.Series(self.cross), 12, 26, 9)
        expected = bool(dif.iloc[-2] <= dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1])
        self.assertEqual(feats_on["macd_cross_up"], expected)

    def test_rsi6_only_computed_when_requested(self):
        off = self.f(_closes_df(self.up), [])
        self.assertIsNone(off["rsi6"])
        on = self.f(_closes_df(self.up), [{"metric": "rsi6_lt", "value": 90}])
        self.assertIsInstance(on["rsi6"], float)

    def test_chip_profit_passthrough(self):
        off = self.f(_closes_df(self.up), [])
        self.assertNotIn("chip_profit", off)
        on = self.f(_closes_df(self.up), [{"metric": "chip_profit_gt", "value": 50}],
                    chip_profit=66.6)
        self.assertEqual(on["chip_profit"], 66.6)
        none_chip = self.f(_closes_df(self.up), [{"metric": "chip_profit_gt", "value": 50}],
                           chip_profit=None)
        self.assertIsNone(none_chip["chip_profit"])


class ScreenerRouteTest(unittest.TestCase):
    """路由 + 落盘: patch _scan_worker, 绝不真跑全量扫描。"""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_screener.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        trades.create_user("scr_admin", "password123", is_admin=True)
        cls.uid = next(u["id"] for u in trades.list_users() if u["username"] == "scr_admin")
        from app import create_app
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    def setUp(self):
        from api import screener
        self.scr = screener
        self._orig_job = dict(screener._job)
        screener._job.update({"running": False, "stop": False, "stopped": False,
                              "progress": 0, "total": 0, "results": [],
                              "conditions": [], "started_at": None, "done_at": None,
                              "error": None})
        # 路由用例统一 patch worker: 即使 Thread 真启动也只是个 Mock, 不扫任何标的
        self._worker = mock.patch.object(screener, "_scan_worker")
        self.worker_mock = self._worker.start()
        self.addCleanup(self._worker.stop)
        self.addCleanup(lambda: screener._job.update(self._orig_job))
        self.client = self.app.test_client()
        token, _ = trades.create_session(self.uid)
        self.client.set_cookie("session", token)
        _csrf(self.client)

    def _post(self, path, body=None):
        return self.client.post(path, data=json.dumps(body or {}),
                                content_type="application/json", headers=_csrf(self.client))

    def test_requires_login(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get("/api/screener/status").status_code, 401)
        self.assertEqual(anon.post("/api/screener/run", data=json.dumps({}),
                                   content_type="application/json").status_code, 401)
        self.assertEqual(anon.post("/api/screener/stop", data=json.dumps({}),
                                   content_type="application/json").status_code, 401)

    def test_run_rejects_invalid_and_empty(self):
        self.assertEqual(self._post("/api/screener/run",
                                    {"conditions": [{"metric": "bogus"}]}).status_code, 400)
        self.assertEqual(self._post("/api/screener/run", {"conditions": []}).status_code, 400)

    def test_run_ok_and_worker_spawned(self):
        r = self._post("/api/screener/run",
                       {"conditions": [{"metric": "above_ma20"}], "count": 5})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        self.worker_mock.assert_called_once()
        # worker 收到清洗后的条件与 count
        self.assertEqual(self.worker_mock.call_args.args[1], 5)

    def test_run_conflict_when_running(self):
        self.scr._job["running"] = True
        self.assertEqual(self._post("/api/screener/run",
                                    {"conditions": [{"metric": "above_ma20"}]}).status_code, 409)
        self.worker_mock.assert_not_called()

    def test_stop_semantics(self):
        r = self._post("/api/screener/stop")
        self.assertFalse(r.get_json()["ok"], "无进行中扫描")
        self.scr._job["running"] = True
        r2 = self._post("/api/screener/stop")
        self.assertTrue(r2.get_json()["ok"])
        self.assertTrue(self.scr._job["stop"])

    def test_status_shape(self):
        self.scr._job.update({"running": True, "progress": 3, "total": 10,
                              "conditions": [{"metric": "above_ma20"}]})
        data = self.client.get("/api/screener/status").get_json()
        for key in ("running", "progress", "total", "results", "conditions", "error"):
            self.assertIn(key, data)
        self.assertEqual(data["progress"], 3)

    def test_persist_and_restore(self):
        last = Path(self._tmpdir.name) / "screener_last.json"
        with mock.patch.object(self.scr, "_last_file", last):
            self.scr._job.update({
                "results": [{"symbol": "600000.SH", "name": "浦发银行", "close": 10.0}],
                "conditions": [{"metric": "above_ma20"}],
                "done_at": 12345.0, "stopped": True,
            })
            self.scr._persist_job()
            self.assertTrue(last.exists())
            # 清空内存 → 从盘恢复
            self.scr._job.update({"results": [], "conditions": [],
                                  "done_at": None, "stopped": False})
            self.scr._load_last_job()
        self.assertEqual(len(self.scr._job["results"]), 1)
        self.assertEqual(self.scr._job["results"][0]["symbol"], "600000.SH")
        self.assertEqual(self.scr._job["conditions"], [{"metric": "above_ma20"}])
        self.assertEqual(self.scr._job["done_at"], 12345.0)
        self.assertTrue(self.scr._job["stopped"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
