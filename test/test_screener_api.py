# -*- coding: utf-8 -*-
"""条件选股 screener 测试: 指标目录 / 任务队列与隔离 / 结果去重 / 因子库扫描。

约束: 任何用例都不真取数 —— 扫描读的是 mock 出来的因子库快照, 外部行情/推送全部
mock; 路由用例只落库 (worker 不在测试里启动)。

运行:
    venv/Scripts/python.exe -u visual/test/test_screener_api.py
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import factors  # noqa: E402
import notify  # noqa: E402
import screener_metrics as metrics  # noqa: E402
import trades  # noqa: E402


def _csrf(client):
    client.get("/login.html")
    headers = {}
    c = client.get_cookie("csrf_token")
    if c is not None:
        headers["X-CSRF-Token"] = c.value if hasattr(c, "value") else str(c)
    return headers


SNAPSHOT = pd.DataFrame([
    {"symbol": "600000.SH", "name": "浦发银行", "close": 10.0, "change_pct": 6.0,
     "above_ma20": True, "rsi6": 15.0, "chip_profit": 80.0, "industry": "银行",
     "pe": 5.0, "float_value": 2.0e11},
    {"symbol": "000001.SZ", "name": "平安银行", "close": 11.0, "change_pct": 1.0,
     "above_ma20": True, "rsi6": 55.0, "chip_profit": 20.0, "industry": "银行",
     "pe": 8.0, "float_value": 3.0e11},
    {"symbol": "300750.SZ", "name": "宁德时代", "close": 300.0, "change_pct": -2.0,
     "above_ma20": False, "rsi6": 18.0, "chip_profit": 30.0, "industry": "电池",
     "pe": 30.0, "float_value": 1.0e12},
])

CONDS_AND = [{"metric": "above_ma20"}, {"metric": "rsi6", "op": "<=", "value": 20}]


class KlineRateTest(unittest.TestCase):
    """选股/因子库拉K令牌: AlphaFeed 日K批量 60/min 的 90% = 54/min。"""

    def test_default_rate_is_ninety_percent(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SCREENER_KLINE_PER_MIN", None)
            self.assertEqual(factors.kline_rate(), 54)
        import af_limits
        self.assertEqual(factors.kline_rate(),
                         af_limits.bucket_rate("kline_daily_batch"))

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"SCREENER_KLINE_PER_MIN": "12"}):
            self.assertEqual(factors.kline_rate(), 12)

    def test_existing_web_buckets_untouched(self):
        import feed
        self.assertEqual(feed.QUOTES_RATE_PER_MIN, 6)
        self.assertEqual(feed.DEPTH_GET_RATE_PER_MIN, 30)

    def test_fetch_bars_uses_rate(self):
        """fetch_bars 必须过令牌桶 (限速在因子库取数这一层)。"""
        import inspect
        src = inspect.getsource(factors.fetch_bars)
        self.assertIn("kline_rate()", src)
        self.assertIn("try_acquire", src)
        # 扫描侧不再自己取数/限速 (因子库模式), 防重复令牌
        from api import screener
        self.assertFalse(hasattr(screener, "_scan_bucket"))


class ScreenerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_screener.db"
        cls._orig_db = trades._db_path
        cls._orig_fac = (factors.CACHE_DIR, factors.DB_PATH, factors.SNAP_DIR)
        trades.init_db(cls._db)
        trades.create_user("scr_admin", "password123", is_admin=True)
        trades.create_user("scr_bob", "password123")
        trades.create_user("scr_carol", "password123")
        cls.uid = {u["username"]: u["id"] for u in trades.list_users()}
        # 因子库指向临时目录, 避免碰到真实 .cache
        factors.CACHE_DIR = Path(cls._tmpdir.name)
        factors.DB_PATH = Path(cls._tmpdir.name) / "factors.db"
        factors.SNAP_DIR = Path(cls._tmpdir.name) / "factors"
        from app import create_app
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        trades._db_path = cls._orig_db
        factors.CACHE_DIR, factors.DB_PATH, factors.SNAP_DIR = cls._orig_fac
        factors._snap_cache.update({"key": None, "df": None, "day": None})
        cls._tmpdir.cleanup()

    def setUp(self):
        conn = trades.get_conn()
        try:
            conn.execute("DELETE FROM screener_runs")
            conn.execute("DELETE FROM monitor_alerts")
            conn.commit()
        finally:
            conn.close()
        # 推送预算是进程级状态: 逐用例清空, 否则跨用例累计会把后续通知全拦掉
        from api import screener as _scr
        with _scr._push_lock:
            _scr._push_log.clear()
        _scr._pending_stop.clear()
        factors._write_snapshot("2026-09-18", SNAPSHOT)
        self.client = self._login("scr_bob")
        self.admin = self._login("scr_admin")

    def _login(self, username):
        c = self.app.test_client()
        token, _ = trades.create_session(self.uid[username])
        c.set_cookie("session", token)
        _csrf(c)
        return c

    def _post(self, path, body=None, client=None):
        c = client or self.client
        return c.post(path, data=json.dumps(body or {}),
                      content_type="application/json", headers=_csrf(c))


class MetricsCatalogTest(ScreenerTestBase):
    def test_requires_login(self):
        self.assertEqual(self.app.test_client().get("/api/screener/metrics").status_code, 401)

    def test_catalog_groups_and_industry_options(self):
        data = self.client.get("/api/screener/metrics").get_json()
        groups = [g["group"] for g in data["groups"]]
        self.assertEqual(groups, ["技术指标", "量价", "基本面/估值", "资金与筹码"])
        self.assertEqual(data["data_version"], "2026-09-18")
        by_key = {m["key"]: m for g in data["groups"] for m in g["metrics"]}
        self.assertEqual(by_key["industry"]["options"], sorted(["银行", "电池"]))
        self.assertEqual(by_key["above_ma20"]["kind"], "bool")
        self.assertEqual(by_key["rsi6"]["unit"], "")


class ScreenerRunRouteTest(ScreenerTestBase):
    def test_run_rejects_bad_body(self):
        self.assertEqual(self._post("/api/screener/run",
                                    {"conditions": [{"metric": "bogus"}]}).status_code, 400)
        self.assertEqual(self._post("/api/screener/run", {"conditions": []}).status_code, 400)
        self.assertEqual(self._post("/api/screener/run",
                                    {"conditions": [{"metric": "above_ma20"}],
                                     "mode": "xor"}).status_code, 400)
        self.assertEqual(self._post("/api/screener/run",
                                    {"conditions": [{"metric": "above_ma20"}],
                                     "price_mode": "future"}).status_code, 400)

    def test_run_creates_queued_row_with_cache_key(self):
        r = self._post("/api/screener/run", {"conditions": CONDS_AND, "mode": "or"})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertFalse(body.get("from_cache"))
        row = trades.get_screener_run(body["run_id"])
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["mode"], "or")
        self.assertEqual(row["user_id"], self.uid["scr_bob"])
        self.assertTrue(row["cache_key"])
        self.assertEqual([c["label"] for c in row["conditions"]], ["站上 MA20", "RSI6"])

    def test_second_run_conflicts(self):
        self.assertEqual(self._post("/api/screener/run",
                                    {"conditions": CONDS_AND}).status_code, 200)
        self.assertEqual(self._post("/api/screener/run",
                                    {"conditions": CONDS_AND}).status_code, 409)
        carol = self._login("scr_carol")
        self.assertEqual(self._post("/api/screener/run", {"conditions": CONDS_AND},
                                    client=carol).status_code, 200)

    def test_no_snapshot_returns_503(self):
        with mock.patch.object(factors, "available_days", lambda: []):
            r = self._post("/api/screener/run", {"conditions": CONDS_AND})
        self.assertEqual(r.status_code, 503)
        self.assertIn("因子库", r.get_json()["error"])

    def test_same_conditions_same_day_reuse_results(self):
        """当日收盘后同条件不再扫: 第二次提交直接复用结果 (from_cache)。"""
        run_id = trades.create_screener_run(
            self.uid["scr_carol"], CONDS_AND, status="done", data_version="2026-09-18",
            cache_key=None)
        key_run = trades.get_screener_run(run_id)
        # 用与请求一致的条件过一遍 clean, 拿到同样的 cache_key
        from api import screener as scr
        from screener_metrics import clean
        clean_conds, _err = clean(CONDS_AND)
        key = scr._cache_key("and", clean_conds, "2026-09-18", "close")
        trades.update_screener_run(run_id, cache_key=key,
                                   results=[{"symbol": "600000.SH", "hit_count": 2}],
                                   total=3)
        r = self._post("/api/screener/run", {"conditions": CONDS_AND})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["from_cache"])
        self.assertEqual(body["n_results"], 1)
        row = trades.get_screener_run(body["run_id"])
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["results"][0]["symbol"], "600000.SH")
        self.assertEqual(row["user_id"], self.uid["scr_bob"], "复用也要落在请求者名下")
        self.assertIsNone(trades.active_screener_run(user_id=self.uid["scr_bob"]))
        self.assertTrue(key_run)   # 原任务未被改动

    def test_different_mode_does_not_reuse(self):
        self._post("/api/screener/run", {"conditions": CONDS_AND, "mode": "and"})
        run = trades.active_screener_run(user_id=self.uid["scr_bob"])
        from api import screener as scr
        from screener_metrics import clean
        clean_conds, _ = clean(CONDS_AND)
        trades.update_screener_run(run["id"], status="done", results=[],
                                   data_version="2026-09-18",
                                   cache_key=scr._cache_key("and", clean_conds,
                                                            "2026-09-18", "close"))
        r = self._post("/api/screener/run", {"conditions": CONDS_AND, "mode": "or"})
        self.assertFalse(r.get_json().get("from_cache"), "不同 mode 不能复用")

    def test_status_isolated_per_user(self):
        run_id = trades.create_screener_run(
            self.uid["scr_bob"], CONDS_AND, status="done", data_version="2026-09-18",
            results=[{"symbol": "600000.SH", "hit_count": 2, "hits": []}], total=3)
        bob = self.client.get("/api/screener/status?full=1").get_json()
        self.assertEqual(bob["current"]["id"], run_id)
        self.assertEqual(bob["factor_day"], "2026-09-18")
        carol = self._login("scr_carol").get("/api/screener/status").get_json()
        self.assertIsNone(carol["current"])
        self.assertEqual(carol["recent"], [])

    def test_status_hides_results_by_default(self):
        """首页徽章每 10~60s 轮询 /status: 默认不能带整块 results (可达数千行)。"""
        trades.create_screener_run(self.uid["scr_bob"], CONDS_AND, status="done",
                                   data_version="2026-09-18",
                                   results=[{"symbol": "600000.SH", "hit_count": 1}],
                                   total=3)
        light = self.client.get("/api/screener/status").get_json()
        self.assertIsNone(light["current"]["results"])
        self.assertEqual(light["current"]["n_results"], 1)
        full = self.client.get("/api/screener/status?full=1").get_json()
        self.assertEqual(len(full["current"]["results"]), 1)

    def test_condition_cap_and_text_validation(self):
        many = [{"metric": "above_ma20"}] * (metrics.MAX_CONDITIONS + 1)
        r = self._post("/api/screener/run", {"conditions": many})
        self.assertEqual(r.status_code, 400)
        self.assertIn("最多", r.get_json()["error"])
        bad = self._post("/api/screener/run",
                         {"conditions": [{"metric": "industry", "value": "不存在的行业"}]})
        self.assertEqual(bad.status_code, 400)
        self.assertIn("可选范围", bad.get_json()["error"])
        long_v = "银" * (metrics.TEXT_MAX_LEN + 1)
        too_long = self._post("/api/screener/run",
                              {"conditions": [{"metric": "industry", "value": long_v}]})
        self.assertIn("取值过长", too_long.get_json()["error"])
        ok = self._post("/api/screener/run",
                        {"conditions": [{"metric": "industry", "value": "银行"}]})
        self.assertEqual(ok.status_code, 200)

    def test_cache_key_covers_cap_and_registry_version(self):
        from api import screener as scr
        from screener_metrics import clean
        conds, _ = clean(CONDS_AND)
        base = scr._cache_key("and", conds, "2026-09-18", "close", 0)
        self.assertNotEqual(base, scr._cache_key("and", conds, "2026-09-18", "close", 300),
                            "改了标的范围不能复用旧结果")
        with mock.patch.object(scr.metrics, "VERSION", "deadbeef"):
            self.assertNotEqual(base, scr._cache_key("and", conds, "2026-09-18", "close", 0),
                                "改了指标口径不能复用旧结果")
        self.assertEqual(base, scr._cache_key("and", conds, "2026-09-18", "close", 0))

    def test_cond_text_uses_mode_joiner(self):
        from api import screener as scr
        from screener_metrics import clean
        conds, _ = clean(CONDS_AND)
        self.assertIn(" 且 ", scr._cond_text(conds, "and"))
        self.assertIn(" 或 ", scr._cond_text(conds, "or"))

    def test_stop_rejects_bad_run_id(self):
        """回归: 非数字 run_id 曾抛 ValueError → 500。"""
        for bad in ("abc", [], {}, 1.9, True, "1e5"):
            r = self._post("/api/screener/stop", {"run_id": bad})
            self.assertEqual(r.status_code, 400, f"run_id={bad!r} 应 400")
        self.assertEqual(self._post("/api/screener/stop", {"run_id": 999999}).status_code, 200)

    def test_status_reports_zero_hit_and_stopped(self):
        zero = trades.create_screener_run(self.uid["scr_bob"], CONDS_AND, status="done",
                                          data_version="2026-09-18", results=[], total=3)
        stopped = trades.create_screener_run(
            self.uid["scr_bob"], CONDS_AND, status="stopped", data_version="2026-09-18",
            results=[{"symbol": "000001.SZ", "hit_count": 1}], total=3)
        data = self.client.get("/api/screener/status").get_json()
        self.assertEqual(data["current"]["id"], stopped)
        self.assertEqual([r["id"] for r in data["recent"]], [stopped, zero])
        self.assertEqual(data["recent"][1]["n_results"], 0, "0 命中也要能看到")
        self.assertTrue(data["recent"][0]["stopped"])
        self.assertIsNone(data["active"])

    def test_history_limited_to_three(self):
        for _ in range(5):
            trades.create_screener_run(self.uid["scr_bob"], CONDS_AND, status="done",
                                       data_version="2026-09-18", results=[])
        data = self.client.get("/api/screener/status").get_json()
        self.assertEqual(len(data["recent"]), 3)
        self.assertEqual(len(trades.list_screener_runs(self.uid["scr_bob"], limit=10)), 5)

    def test_prune_keeps_recent_and_active(self):
        for _ in range(12):
            trades.create_screener_run(self.uid["scr_bob"], CONDS_AND, status="done")
        trades.create_screener_run(self.uid["scr_bob"], CONDS_AND, status="queued")
        trades.prune_screener_runs(self.uid["scr_bob"], keep=10)
        self.assertEqual(len(trades.list_screener_runs(self.uid["scr_bob"], limit=50)), 10)
        self.assertEqual(trades.active_screener_run(self.uid["scr_bob"])["status"], "queued")

    def test_stop_before_worker_claims_is_not_lost(self):
        """回归: 认领(running)与写内存态之间有窗口, 这期间取消不能丢。"""
        from api import screener as scr
        conds, _err = scr.metrics.clean(CONDS_AND)
        run_id = trades.create_screener_run(self.uid["scr_bob"], conds)
        trades.claim_screener_run(run_id)       # 已 running, 但 _current["id"] 还是 None
        r = self._post("/api/screener/stop", {"run_id": run_id})
        self.assertTrue(r.get_json()["ok"])
        self.assertIn(run_id, scr._pending_stop, "窗口内的取消要先记账")
        with mock.patch.object(notify, "notify"):
            scr._execute(trades.get_screener_run(run_id))
        self.assertEqual(trades.get_screener_run(run_id)["status"], "stopped")
        self.assertNotIn(run_id, scr._pending_stop)

    def test_stop_semantics(self):
        run_id = trades.create_screener_run(self.uid["scr_bob"], CONDS_AND)
        self.assertTrue(self._post("/api/screener/stop", {"run_id": run_id}).get_json()["ok"])
        self.assertEqual(trades.get_screener_run(run_id)["status"], "stopped")
        self.assertFalse(self._post("/api/screener/stop",
                                    {"run_id": run_id}).get_json()["ok"])
        self.assertFalse(self._post("/api/screener/stop").get_json()["ok"], "无进行中任务")

    def test_cannot_stop_other_users_run(self):
        carol_run = trades.create_screener_run(self.uid["scr_carol"], CONDS_AND)
        self.assertEqual(self._post("/api/screener/stop",
                                    {"run_id": carol_run}).status_code, 403)
        self.assertTrue(self._post("/api/screener/stop", {"run_id": carol_run},
                                   client=self.admin).get_json()["ok"])
        self.assertEqual(trades.get_screener_run(carol_run)["status"], "stopped")

    def test_detail_permissions(self):
        run_id = trades.create_screener_run(
            self.uid["scr_bob"], CONDS_AND, status="done",
            results=[{"symbol": "600000.SH", "hit_count": 2}])
        self.assertEqual(self.client.get(f"/api/screener/runs/{run_id}").status_code, 200)
        self.assertEqual(self._login("scr_carol").get(
            f"/api/screener/runs/{run_id}").status_code, 403)
        self.assertEqual(self.admin.get(f"/api/screener/runs/{run_id}").status_code, 200)
        self.assertEqual(self.client.get("/api/screener/runs/999999").status_code, 404)

    def test_admin_queue_view(self):
        trades.create_screener_run(self.uid["scr_bob"], CONDS_AND, mode="or")
        trades.create_screener_run(self.uid["scr_carol"], CONDS_AND, status="done",
                                   results=[{"symbol": "X"}])
        self.assertEqual(self.client.get("/api/admin/screener/runs").status_code, 403)
        data = self.admin.get("/api/admin/screener/runs").get_json()
        self.assertEqual([q["username"] for q in data["queue"]], ["scr_bob"])
        self.assertEqual(data["queue"][0]["mode"], "or")
        self.assertEqual([r["username"] for r in data["recent"]], ["scr_carol"])


class ScreenerScanTest(ScreenerTestBase):
    """执行路径: 读 mock 因子快照 → AND/OR 判定 → 结果/明细/通知。绝不取数。"""

    def setUp(self):
        super().setUp()
        self._snap = mock.patch.object(factors, "snapshot",
                                       lambda day=None: (SNAPSHOT.copy(), "2026-09-18"))
        self._snap.start()
        self.addCleanup(self._snap.stop)

    def _run_scan(self, conditions, mode="and", price_mode="close"):
        from api import screener as scr
        conds, err = scr.metrics.clean(conditions)
        assert err is None, err
        run_id = trades.create_screener_run(self.uid["scr_bob"], conds, mode=mode,
                                            price_mode=price_mode)
        trades.claim_screener_run(run_id)
        run = trades.get_screener_run(run_id)
        notifications = []
        with mock.patch.object(notify, "notify",
                               side_effect=lambda t, x, **k: notifications.append((t, x, k))):
            scr._execute(run)
        return trades.get_screener_run(run_id), notifications

    def test_and_scan_results_and_details(self):
        row, notes = self._run_scan(CONDS_AND)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["total"], 3)
        self.assertEqual(row["progress"], 3)
        self.assertEqual(row["data_version"], "2026-09-18")
        self.assertEqual([r["symbol"] for r in row["results"]], ["600000.SH"])
        res = row["results"][0]
        self.assertEqual(res["hit_count"], 2)
        self.assertEqual([h["label"] for h in res["hits"]], ["站上 MA20", "RSI6"])
        self.assertEqual([h["ok"] for h in res["hits"]], [True, True])
        self.assertEqual(res["hits"][1]["value"], 15.0)
        self.assertEqual(res["name"], "浦发银行")
        titles = [n[0] for n in notes]
        self.assertIn("选股开始", titles)
        self.assertIn("选股完成: 命中 1 只", titles[-1])
        self.assertIn("2026-09-18", notes[0][1])

    def test_or_scan_counts_hits(self):
        row, notes = self._run_scan(CONDS_AND, mode="or")
        got = {r["symbol"]: r["hit_count"] for r in row["results"]}
        self.assertEqual(got, {"600000.SH": 2, "000001.SZ": 1, "300750.SZ": 1})
        self.assertIn("满足任一", notes[-1][1])
        detail = {(r["symbol"], h["label"]): h["ok"]
                  for r in row["results"] for h in r["hits"]}
        self.assertFalse(detail[("300750.SZ", "站上 MA20")])
        self.assertTrue(detail[("300750.SZ", "RSI6")])

    def test_range_and_text_conditions(self):
        row, _n = self._run_scan([
            {"metric": "industry", "value": "银行"},
            {"metric": "float_value_yi", "value": 100, "value2": 2500},
        ])
        self.assertEqual([r["symbol"] for r in row["results"]], ["600000.SH"])
        hits = row["results"][0]["hits"]
        self.assertEqual(hits[0]["value"], "银行")
        self.assertAlmostEqual(hits[1]["value"], 2000.0)

    def test_no_snapshot_marks_error(self):
        with mock.patch.object(factors, "snapshot", lambda day=None: (None, None)):
            row, _n = self._run_scan(CONDS_AND)
        self.assertEqual(row["status"], "error")
        self.assertIn("因子库", row["error"])

    def test_stop_midway_keeps_partial(self):
        big = pd.concat([SNAPSHOT] * 200, ignore_index=True)
        with mock.patch.object(factors, "snapshot", lambda day=None: (big, "2026-09-18")):
            from api import screener as scr
            conds, _e = scr.metrics.clean(CONDS_AND)
            run_id = trades.create_screener_run(self.uid["scr_bob"], conds)
            trades.claim_screener_run(run_id)
            with scr._job_lock:
                scr._current.update({"id": run_id, "stop": False})
            orig = scr.trades.update_screener_run

            def stop_on_progress(rid, **fields):
                if fields.get("progress"):
                    with scr._job_lock:
                        scr._current.update({"id": rid, "stop": True})
                return orig(rid, **fields)

            with mock.patch.object(scr.trades, "update_screener_run", stop_on_progress), \
                 mock.patch.object(notify, "notify"):
                scr._execute(trades.get_screener_run(run_id))
        row = trades.get_screener_run(run_id)
        self.assertEqual(row["status"], "stopped")
        self.assertGreater(row["progress"], 0)
        self.assertLess(row["progress"], len(big))

    def test_live_mode_falls_back_when_unavailable(self):
        with mock.patch.object(factors, "live_snapshot", lambda max_age_sec=None: (None, None)):
            row, _n = self._run_scan(CONDS_AND, price_mode="live")
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["data_version"], "2026-09-18", "回退收盘口径")
        self.assertEqual(row["price_mode"], "close", "回退后口径要如实记为收盘")

    def test_live_fallback_result_is_reused_by_next_live_run(self):
        """回归: 回退收盘的结果, 第二次 live 请求应当命中同一份缓存而不是重扫。"""
        from api import screener as scr
        with mock.patch.object(factors, "live_snapshot", lambda max_age_sec=None: (None, None)):
            first, _n1 = self._run_scan(CONDS_AND, price_mode="live")
        with mock.patch.object(factors, "live_snapshot", lambda max_age_sec=None: (None, None)), \
             mock.patch.object(scr, "_scan") as scan:
            second, notes = self._run_scan(CONDS_AND, price_mode="live")
        scan.assert_not_called()
        self.assertEqual(second["status"], "done")
        self.assertEqual(len(second["results"]), len(first["results"]))
        self.assertEqual(second["cache_key"], first["cache_key"])
        self.assertIn("复用", notes[-1][1])

    def test_truncation_is_flagged(self):
        """回归: 结果被单次上限截断时必须显式标记 (不能和"共 N 只"混为一谈)。"""
        from api import screener as scr
        with mock.patch.object(scr, "RESULT_MAX", 1):
            row, notes = self._run_scan([{"metric": "above_ma20"}], mode="or")
        self.assertEqual(len(row["results"]), 1)
        self.assertTrue(row["truncated"])
        self.assertIn("上限", notes[-1][1])
        data = self.client.get("/api/screener/status").get_json()
        self.assertTrue(data["current"]["truncated"])
        self.assertEqual(data["current"]["n_results"], 1)

    def test_push_budget_limits_scan_notifications(self):
        """回归: 扫描推送受每小时预算约束 (改一个阈值就能绕过去重, 否则可刷群)。"""
        from api import screener as scr
        with mock.patch.object(scr, "PUSH_MAX_PER_HOUR", 1), \
             mock.patch.object(scr, "_push_log", {}):
            _row, notes = self._run_scan([{"metric": "above_ma20"}], mode="or")
        self.assertEqual([t for t, _x, _k in notes], ["选股开始"], "完成推送被预算拦下")
        alerts = trades.list_monitor_alerts(self.uid["scr_bob"])
        self.assertIn("screener_done", {a["alert_type"] for a in alerts}, "站内告警不受预算影响")

    def test_live_mode_uses_patched_snapshot(self):
        live = SNAPSHOT.copy()
        live.loc[live["symbol"] == "300750.SZ", "above_ma20"] = True
        with mock.patch.object(factors, "live_snapshot",
                               lambda max_age_sec=None: (live, "2026-09-18+live@99")):
            row, _n = self._run_scan([{"metric": "above_ma20"}], price_mode="live")
        self.assertEqual(row["data_version"], "2026-09-18+live@99")
        self.assertEqual(row["price_mode"], "live")
        self.assertEqual(len(row["results"]), 3)

    def test_max_symbols_cap(self):
        with mock.patch.dict(os.environ, {"SCREENER_MAX_SYMBOLS": "1"}):
            row, _n = self._run_scan(CONDS_AND, mode="or")
        self.assertEqual(row["total"], 1)


class FactorsRouteTest(ScreenerTestBase):
    """因子库状态/手动重建路由 (选股页状态条与管理页按钮用)。"""

    def test_status_requires_login(self):
        self.assertEqual(self.app.test_client().get("/api/factors/status").status_code, 401)

    def test_status_shape(self):
        factors.init_store()
        factors._ensure_build_row("2026-09-18")
        factors._update_build("2026-09-18", state="done", n_rows=3, n_symbols=3, percent=100)
        data = self.client.get("/api/factors/status").get_json()
        self.assertEqual(data["snapshot_day"], "2026-09-18")
        self.assertEqual(len(data["last_days"]), 5)
        self.assertEqual(data["last_days"][0]["state"], "done")
        self.assertIn("build_at", data)

    def test_rebuild_admin_only_and_runs_in_background(self):
        self.assertEqual(self._post("/api/factors/rebuild").status_code, 403)
        started = {}
        with mock.patch.object(factors, "build",
                               side_effect=lambda day=None, force=False: started.update(
                                   {"day": day, "force": force})):
            r = self._post("/api/factors/rebuild", {"force": True}, client=self.admin)
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()["started"])
            for _ in range(50):        # 等后台线程真的调一次 build
                if started:
                    break
                time.sleep(0.02)
        self.assertTrue(started.get("force"), "force 应透传给 build")

    def test_rebuild_reports_blocked_reason(self):
        """回归: 开不了跑时不能回 started:true (管理员会以为已触发)。"""
        factors.init_store()
        factors._ensure_build_row("2026-09-18")
        factors._update_build("2026-09-18", state="done", n_rows=1)
        with mock.patch.object(factors, "due_day", lambda now=None: "2026-09-18"), \
             mock.patch.object(factors, "build") as b:
            r = self._post("/api/factors/rebuild", {}, client=self.admin)
            body = r.get_json()
            time.sleep(0.05)
        self.assertFalse(body["ok"])
        self.assertFalse(body["started"])
        self.assertIn("已构建完成", body["reason"])
        b.assert_not_called()

    def test_factor_status_hides_error_detail_from_normal_user(self):
        with mock.patch.object(factors, "status",
                               lambda: {"state": "error", "snapshot_day": None, "last_days": [],
                                        "error": "上游 https://internal.example/x 超时"}):
            user_view = self.client.get("/api/factors/status").get_json()
            admin_view = self.admin.get("/api/factors/status").get_json()
        self.assertNotIn("internal.example", user_view["error"])
        self.assertIn("internal.example", admin_view["error"])


class ScreenerWorkerTest(ScreenerTestBase):
    def test_process_next_reaps_stale_running(self):
        from api import screener as scr
        run_id = trades.create_screener_run(self.uid["scr_carol"], CONDS_AND)
        conn = trades.get_conn()
        try:
            conn.execute("UPDATE screener_runs SET status='running' WHERE id=?", (run_id,))
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(scr._process_next())
        row = trades.get_screener_run(run_id)
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error"], "服务重启中断")

    def test_stopped_queued_run_not_executed(self):
        from api import screener as scr
        run_id = trades.create_screener_run(self.uid["scr_carol"], CONDS_AND)
        trades.stop_screener_run(run_id)
        self.assertFalse(scr._process_next())
        self.assertEqual(trades.get_screener_run(run_id)["status"], "stopped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
