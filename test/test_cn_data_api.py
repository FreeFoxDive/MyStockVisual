# -*- coding: utf-8 -*-
"""A 股特色数据 (/api/cn/*) 测试: 列映射/清洗/缓存/重试 + 路由鉴权与市场分支。

akshare 用假模块注入 (patch.dict sys.modules), 不 import 真包、不联网;
资金流走自建 requests 请求 (东财 push2his→push2delay), 用 mock.patch requests.get 拦截;
重试用例把 cn_data.time.sleep 换成 no-op, 避免 0.8+1.6s 真实等待。

运行:
    venv/Scripts/python.exe -u visual/test/test_cn_data_api.py
"""

import json
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import pandas as pd
import requests

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


def _fake_ak(**fns):
    """构造假 akshare 模块 (SimpleNamespace), 属性按需替换为 Mock。"""
    return types.SimpleNamespace(**fns)


class HelpersTest(unittest.TestCase):
    """_clean_cell / _rows_pick / _cn_market: 纯函数。"""

    def setUp(self):
        from api import cn_data
        self.cd = cn_data

    def test_clean_cell(self):
        self.assertIsNone(self.cd._clean_cell(None))
        self.assertIsNone(self.cd._clean_cell(float("nan")))
        self.assertIsNone(self.cd._clean_cell(pd.NA))
        out = self.cd._clean_cell(pd.np.float64(1.5)) if hasattr(pd, "np") else self.cd._clean_cell(1.5)
        self.assertEqual(out, 1.5)

    def test_rows_pick_mapping_limit_and_sort(self):
        df = pd.DataFrame({
            "日期": ["2026-01-01", "2026-01-03", "2026-01-02"],
            "收盘价": [1.0, 2.0, 3.0],
            "无关列": [9, 9, 9],
        })
        out = self.cd._rows_pick(df, [("日期", "日期"), ("收盘价", "收盘")],
                                 limit=2, date_key="日期")
        self.assertEqual(out, [{"日期": "2026-01-03", "收盘": 2.0},
                               {"日期": "2026-01-02", "收盘": 3.0}])

    def test_rows_pick_falls_back_to_first_cols(self):
        df = pd.DataFrame({"a": [1], "b": [2], "c": [3], "d": [4], "e": [5]})
        out = self.cd._rows_pick(df, [("不存在的列", "x")])
        self.assertEqual(out, [{"a": 1, "b": 2, "c": 3, "d": 4}])

    def test_rows_pick_skips_all_none_rows(self):
        # 只要有一个字段非空即保留; 整行全空才丢弃
        df = pd.DataFrame({
            "日期": ["2026-01-01", "2026-01-02", None],
            "值": [float("nan"), 5.0, float("nan")],
        })
        out = self.cd._rows_pick(df, [("日期", "日期"), ("值", "值")], date_key="日期")
        self.assertEqual(out, [{"日期": "2026-01-02", "值": 5.0},
                               {"日期": "2026-01-01", "值": None}])

    def test_rows_pick_empty(self):
        self.assertEqual(self.cd._rows_pick(pd.DataFrame(), [("x", "y")]), [])
        self.assertEqual(self.cd._rows_pick(None, [("x", "y")]), [])

    def test_cn_market(self):
        self.assertEqual(self.cd._cn_market("600000.SH"), "sh")
        self.assertEqual(self.cd._cn_market("430047.BJ"), "bj")
        self.assertEqual(self.cd._cn_market("000001.SZ"), "sz")


class CnRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_cn.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        trades.create_user("cn_u", "password123")
        cls.uid = trades.list_users()[0]["id"]
        from app import create_app
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    def setUp(self):
        from api import cn_data
        self.cd = cn_data
        cn_data._cache.clear()
        self.addCleanup(cn_data._cache.clear)
        cn_data._fail_ts.clear()
        self.addCleanup(cn_data._fail_ts.clear)
        self.client = self.app.test_client()
        token, _ = trades.create_session(self.uid)
        self.client.set_cookie("session", token)
        _csrf(self.client)
        # 不真正 sleep (失败重试用例)
        self._time_patch = mock.patch.object(
            cn_data, "time", types.SimpleNamespace(time=time.time, sleep=lambda *_: None))
        self._time_patch.start()
        self.addCleanup(self._time_patch.stop)

    CASES = (("fund-flow", True), ("lhb", True), ("dividends", True),
             ("announcements", True))

    def test_requires_login(self):
        anon = self.app.test_client()
        for name, _needs_symbol in self.CASES:
            self.assertEqual(anon.get(f"/api/cn/{name}").status_code, 401, name)

    def test_missing_symbol(self):
        for name, needs_symbol in self.CASES:
            if needs_symbol:
                self.assertEqual(self.client.get(f"/api/cn/{name}").status_code, 400, name)

    def test_hk_us_short_circuits_without_akshare(self):
        ak = _fake_ak(stock_individual_fund_flow=mock.Mock(side_effect=AssertionError("不应调用")))
        with mock.patch.dict(sys.modules, {"akshare": ak}):
            for name in ("fund-flow", "dividends", "announcements"):
                data = self.client.get(f"/api/cn/{name}?symbol=00700.HK").get_json()
                self.assertEqual(data["rows"], [], name)
                self.assertIn("港", data["hint"], name)

    def test_fund_flow_maps_and_caches(self):
        df = pd.DataFrame({
            "日期": ["2026-09-10", "2026-09-11"],
            "收盘价": [10.0, 10.5],
            "涨跌幅": [1.0, 5.0],
            "主力净流入-净额": [100.0, 200.0],
            "主力净流入-净占比": [1.5, 2.5],
            "超大单净流入-净额": [10.0, 20.0],
        })
        fetch = mock.Mock(return_value=(df, False))
        with mock.patch.object(self.cd, "_fetch_fund_flow", fetch):
            data = self.client.get("/api/cn/fund-flow?symbol=600000.SH").get_json()
            first = fetch.call_count
            again = self.client.get("/api/cn/fund-flow?symbol=600000.SH").get_json()
        self.assertEqual(data["rows"][0]["日期"], "2026-09-11", "日期倒序")
        self.assertEqual(data["rows"][0]["主力净额(万)"], 200.0)
        self.assertEqual(data["rows"][0]["超大单净额(万)"], 20.0)
        self.assertNotIn("hint", data, "history 源正常时不应有降级提示")
        self.assertEqual(again, data)
        self.assertEqual(fetch.call_count, first, "第二次应命中缓存")
        self.assertEqual(fetch.call_args.args, ("600000", "sh"))

    def test_fund_flow_falls_back_to_delay_host(self):
        """push2his 不可达时回退 push2delay, 仅最新一日并带降级提示。"""
        klines = ["2026-09-11,61362694,17907632,-79270323,46678569,14684125,6.77,1.98,-8.75,5.15,1.62,35.50,-1.03,0,0"]
        hosts = []

        def fake_get(url, **kw):
            hosts.append(url.split("/")[2])
            if "push2his" in url:
                raise requests.exceptions.ConnectionError("RemoteDisconnected")
            return mock.Mock(status_code=200, raise_for_status=lambda: None,
                             json=lambda: {"rc": 0, "data": {"klines": klines}})

        with mock.patch("requests.get", side_effect=fake_get):
            df, degraded = self.cd._fetch_fund_flow("002142", "sz")
        self.assertTrue(degraded)
        self.assertEqual(hosts, ["push2his.eastmoney.com", "push2delay.eastmoney.com"])
        row = df.iloc[0]
        self.assertEqual(row["日期"], "2026-09-11")
        self.assertAlmostEqual(row["主力净流入-净额"], 6136.2694, places=4, msg="元→万元")
        self.assertAlmostEqual(row["超大单净流入-净额"], 1468.4125, places=4)
        self.assertAlmostEqual(row["主力净流入-净占比"], 6.77, places=4)

    def test_fund_flow_both_hosts_fail_returns_error(self):
        def fake_get(url, **kw):
            raise requests.exceptions.ConnectionError("RemoteDisconnected")

        with mock.patch("requests.get", side_effect=fake_get):
            data = self.client.get("/api/cn/fund-flow?symbol=600000.SH").get_json()
        self.assertIn("error", data)
        self.assertEqual(data["error"], "数据源暂时不可用", "对外文案不应含异常原文")

    def test_dividends_shows_description_and_transfer_ratio(self):
        """分红列显示东财的描述文本 (每10股口径), 且送转比例列不再因列名不匹配而丢失。"""
        df = pd.DataFrame({
            "报告期": ["2025-12-31", "2026-06-30"],
            "送转股份-送转总比例": [0.0, 5.0],
            "现金分红-现金分红比例": [3.5, 2.0],
            "现金分红-现金分红比例描述": ["10派3.50元(含税)", "10派2.00元(含税)"],
            "最新公告日期": ["2026-04-20", "2026-08-20"],
            "除权除息日": ["2026-05-12", "2026-09-12"],
        })
        with mock.patch.dict(sys.modules, {"akshare": _fake_ak(
                stock_fhps_detail_em=mock.Mock(return_value=df))}):
            data = self.client.get("/api/cn/dividends?symbol=688617.SH").get_json()
        row = data["rows"][0]
        self.assertEqual(row["现金分红"], "10派2.00元(含税)", "应为描述文本而非裸数字")
        self.assertNotIn("3.5", str(row["现金分红"]))
        self.assertEqual(row["送转比例"], 5.0, "送转比例列必须存在 (旧列名不匹配 bug)")
        self.assertEqual(row["公告日期"], "2026-08-20", "按公告日期倒序")
        self.assertEqual(len(data["rows"]), 2)

    def test_lhb_passes_date_window_and_filters_code(self):
        df = pd.DataFrame({
            "代码": ["600000", "000001"],
            "上榜日": ["2026-09-10", "2026-09-11"],
            "解读": ["a", "b"],
            "收盘价": [10.0, 11.0],
            "涨跌幅": [1.0, 2.0],
            "龙虎榜净买额": [100.0, 200.0],
        })
        fetch = mock.Mock(return_value=df)
        with mock.patch.object(self.cd.market_hours, "now", return_value=datetime(2026, 9, 13)), \
             mock.patch.dict(sys.modules, {"akshare": _fake_ak(stock_lhb_detail_em=fetch)}):
            data = self.client.get("/api/cn/lhb?symbol=000001.SZ").get_json()
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["rows"][0]["收盘"], 11.0)
        kwargs = fetch.call_args.kwargs
        self.assertEqual(kwargs["start_date"], "20260906")
        self.assertEqual(kwargs["end_date"], "20260913")

    def test_lhb_empty_returns_hint(self):
        with mock.patch.dict(sys.modules, {"akshare": _fake_ak(
                stock_lhb_detail_em=mock.Mock(return_value=pd.DataFrame()))}):
            data = self.client.get("/api/cn/lhb?symbol=600000.SH").get_json()
        self.assertEqual(data["rows"], [])
        self.assertIn("无龙虎榜", data["hint"])

    def test_source_failure_returns_error_dict(self):
        # 北向 tab 已下线, 拿龙虎榜做重试样例
        fetch = mock.Mock(side_effect=RuntimeError("RemoteDisconnected"))
        with mock.patch.dict(sys.modules, {"akshare": _fake_ak(stock_lhb_detail_em=fetch)}):
            data = self.client.get("/api/cn/lhb?symbol=600000.SH").get_json()
        self.assertIn("error", data)
        self.assertEqual(fetch.call_count, 3, "应重试 3 次")

    def test_stale_cache_fallback_on_failure(self):
        good = pd.DataFrame({"日期": ["2026-09-11"], "收盘价": [10.0],
                             "主力净流入-净额": [1.0]})
        fetch = mock.Mock(return_value=(good, False))
        with mock.patch.object(self.cd, "_fetch_fund_flow", fetch):
            first = self.client.get("/api/cn/fund-flow?symbol=600000.SH").get_json()
        self.cd._cache["ff:600000.SH"] = (time.time() - self.cd.CACHE_TTL - 1, first)  # 置为过期
        fetch.side_effect = RuntimeError("boom")
        with mock.patch.object(self.cd, "_fetch_fund_flow", fetch):
            again = self.client.get("/api/cn/fund-flow?symbol=600000.SH").get_json()
        self.assertEqual(again, first, "过期后拉取失败应回退旧缓存")

    def test_negative_cache_skips_retry_within_fail_ttl(self):
        """失败后负缓存窗口内: 同 key 请求不再重试 (保护挂掉的上游), 直接报错。"""
        fetch = mock.Mock(side_effect=RuntimeError("RemoteDisconnected"))
        with mock.patch.dict(sys.modules, {"akshare": _fake_ak(stock_lhb_detail_em=fetch)}):
            first = self.client.get("/api/cn/lhb?symbol=600000.SH").get_json()
            self.assertIn("error", first)
            self.assertEqual(fetch.call_count, 3, "首次失败应重试 3 次")
            again = self.client.get("/api/cn/lhb?symbol=600000.SH").get_json()
        self.assertIn("error", again)
        self.assertEqual(fetch.call_count, 3, "负缓存窗口内不应再次发起重试")

    def test_negative_cache_expires_after_fail_ttl(self):
        """负缓存窗口过后恢复正常重试。"""
        fetch = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch.dict(sys.modules, {"akshare": _fake_ak(stock_lhb_detail_em=fetch)}):
            self.client.get("/api/cn/lhb?symbol=600000.SH").get_json()
            self.assertEqual(fetch.call_count, 3)
            # 把失败时间戳拨回 FAIL_TTL 之前
            self.cd._fail_ts["lhb:600000.SH"] = time.time() - self.cd.FAIL_TTL - 1
            self.client.get("/api/cn/lhb?symbol=600000.SH").get_json()
        self.assertEqual(fetch.call_count, 6, "窗口过后应重新重试")


if __name__ == "__main__":
    unittest.main(verbosity=2)
