# -*- coding: utf-8 -*-
"""麦蕊市场数据路由测试 (/api/cn/zljlr|exchange-announcement|holder-change|top-holders|float-holders|unlock)。

market.get_mr 注入假客户端 (SimpleNamespace), 不联网; 数据层 TTL 缓存每个用例前清空。
运行:
    venv/Scripts/python.exe -u visual/test/test_mr_data_api.py
"""
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import market  # noqa: E402
import security  # noqa: E402
import trades  # noqa: E402

CASES = ("zljlr", "exchange-announcement", "holder-change",
         "top-holders", "float-holders", "unlock")


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


class MrDataRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_mr.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        trades.create_user("mr_u", "password123")
        cls.uid = trades.list_users()[0]["id"]
        from app import create_app
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    @staticmethod
    def _clear():
        market._mr_market_cache.clear()
        market._mr_company_cache.clear()

    def setUp(self):
        self._clear()
        self.addCleanup(self._clear)
        _reset_rate_limit()
        self.client = self.app.test_client()
        token, _ = trades.create_session(self.uid)
        self.client.set_cookie("session", token)
        _csrf(self.client)

    @staticmethod
    def _fake(**kw):
        return types.SimpleNamespace(**kw)

    def test_requires_login(self):
        anon = self.app.test_client()
        for name in CASES:
            self.assertEqual(anon.get(f"/api/cn/{name}?symbol=000001.SZ").status_code, 401, name)

    def test_missing_symbol(self):
        for name in CASES:
            self.assertEqual(self.client.get(f"/api/cn/{name}").status_code, 400, name)

    def test_non_cn_hint(self):
        mr = self._fake(hsdc_zljlr=mock.Mock(side_effect=AssertionError("不应调用")),
                        stock_announcement=mock.Mock(side_effect=AssertionError("不应调用")))
        with mock.patch.object(market, "get_mr", return_value=mr):
            for name in CASES:
                data = self.client.get(f"/api/cn/{name}?symbol=00700.HK").get_json()
                if name == "zljlr":
                    continue  # 全市场榜单对港/美股也返回
                self.assertEqual(data["rows"], [], name)
                self.assertIn("港", data["hint"], name)

    def test_zljlr_single_stock_kv(self):
        rows = [
            {"dm": f"{i:06d}", "mc": f"股{i}", "zxj": 10.0 + i, "zdf": 1.0,
             "cje": 1e9, "hsl": 2.0, "jlr": (40 - i) * 1e8, "jlrl": 1.1,
             "zllrzj": (40 - i) * 1e8, "zllczj": 0.0,
             "zljlr": (40 - i) * 1e8, "zljlrl": 1.5, "t": "2026-09-1116:12:01"}
            for i in range(40)
        ]
        mr = self._fake(hsdc_zljlr=lambda: rows)
        with mock.patch.object(market, "get_mr", return_value=mr):
            data = self.client.get("/api/cn/zljlr?symbol=000035.SZ").get_json()
        kv = dict(data["kv"])
        self.assertNotIn("rows", data)
        self.assertEqual(kv["主力净流入额(亿)"], 5.0, "元 → 亿")
        self.assertEqual(kv["主力净流入率%"], 1.5)
        self.assertEqual(kv["主力流入(亿)"], 5.0)
        self.assertEqual(kv["主力流出(亿)"], 0.0)
        self.assertEqual(kv["全市场排名"], "36/40")
        self.assertEqual(kv["数据时间"], "2026-09-11 16:12:01", "t 补空格")
        self.assertNotIn("成交额(亿)", kv, "该接口 cje 恒为 0, 不展示")
        self.assertNotIn("换手率%", kv)

    def test_zljlr_stock_not_in_list(self):
        mr = self._fake(hsdc_zljlr=lambda: [{"dm": "000001", "zljlr": 1e8}])
        with mock.patch.object(market, "get_mr", return_value=mr):
            data = self.client.get("/api/cn/zljlr?symbol=999999.SZ").get_json()
        self.assertEqual(data["kv"], [])
        self.assertIn("不在", data["hint"])

    def test_exchange_announcement_sorted_desc(self):
        # 源为升序 (最早在前) → 输出应倒序 (最新在前)
        mr = self._fake(stock_announcement=lambda code, lt=20: [
            {"t": "2025-04-30", "zt": "旧公告", "nr": "http://x/old.pdf", "gs": "TXT"},
            {"t": "2026-08-01", "zt": "新公告", "nr": "http://x/new.pdf", "gs": "TXT"},
        ])
        with mock.patch.object(market, "get_mr", return_value=mr):
            data = self.client.get("/api/cn/exchange-announcement?symbol=000001.SZ").get_json()
        self.assertEqual([r["日期"] for r in data["rows"]], ["2026-08-01", "2025-04-30"])
        self.assertEqual(data["rows"][0], {"日期": "2026-08-01", "标题": "新公告",
                                           "链接": "http://x/new.pdf"})

    def test_holder_change(self):
        mr = self._fake(company_holder_change=lambda code: [
            {"jzrq": "2026-06-30", "gdhs": "450712", "bh": "减少6898"}])
        with mock.patch.object(market, "get_mr", return_value=mr):
            data = self.client.get("/api/cn/holder-change?symbol=000001.SZ").get_json()
        self.assertEqual(data["rows"][0]["股东户数"], 450712, "字符串户数 → int")
        self.assertEqual(data["rows"][0]["增减"], "减少6898", "增减文本原样保留")

    def test_holder_change_sorted_newest_first(self):
        # 上游顺序不作契约: 接口保证按截止日期倒序 (前端折线图自行升序)
        mr = self._fake(company_holder_change=lambda code: [
            {"jzrq": "2025-12-31", "gdhs": "1", "bh": "增加1"},
            {"jzrq": "2026-06-30", "gdhs": "3", "bh": "增加1"},
            {"jzrq": "2026-03-31", "gdhs": "2", "bh": "增加1"},
        ])
        with mock.patch.object(market, "get_mr", return_value=mr):
            data = self.client.get("/api/cn/holder-change?symbol=000001.SZ").get_json()
        self.assertEqual([r["截止日期"] for r in data["rows"]],
                         ["2026-06-30", "2026-03-31", "2025-12-31"])

    def test_holder_change_normalizes_non_iso_dates(self):
        # 前端的「近三年」窗口是按 ISO 字符串比较的: 数据源换格式时必须卡在出口归一,
        # 否则窗口判断会静默失效 (例如 "2025/01/05" >= "2025-09-19" 为真)。
        mr = self._fake(company_holder_change=lambda code: [
            {"jzrq": "2025/01/05", "gdhs": "1", "bh": "增加1"},
            {"jzrq": "20250630", "gdhs": "3", "bh": "增加1"},
            {"jzrq": "2025-03-31 00:00:00", "gdhs": "2", "bh": "增加1"},
            {"jzrq": "—", "gdhs": "4", "bh": "增加1"},
            {"jzrq": "2025-13-45", "gdhs": "5", "bh": "增加1"},
        ])
        with mock.patch.object(market, "get_mr", return_value=mr):
            data = self.client.get("/api/cn/holder-change?symbol=000001.SZ").get_json()
        self.assertEqual([r["截止日期"] for r in data["rows"]],
                         ["2025-06-30", "2025-03-31", "2025-01-05", None, None],
                         "斜杠/紧凑/带时间都要归一成 ISO; 认不出的给 None 并排到末尾")

    def test_top_holders_flatten_latest_report(self):
        reports = [
            {"jzrq": "2026-06-30", "ggrq": "2026-08-15", "sdgd": [
                {"pm": 1, "gdmc": "平安集团", "cgsl": 9618540236, "cgbl": 49.56, "gbxz": "流通A股"},
                {"pm": 2, "gdmc": "平安人寿", "cgsl": 1186100488, "cgbl": 6.11, "gbxz": "流通A股"},
            ]},
            {"jzrq": "2026-03-31", "ggrq": "2026-04-25", "sdgd": [
                {"pm": 1, "gdmc": "旧", "cgsl": 1, "cgbl": 0.1, "gbxz": "A股"},
            ]},
        ]
        mr = self._fake(company_top10_holders=lambda code: reports)
        with mock.patch.object(market, "get_mr", return_value=mr):
            data = self.client.get("/api/cn/top-holders?symbol=000001.SZ").get_json()
        self.assertEqual(len(data["rows"]), 2, "仅最新报告期")
        self.assertEqual(data["rows"][0]["报告期"], "2026-06-30")
        self.assertEqual(data["rows"][0]["持股数(万股)"], 961854.02)
        self.assertIn("报告期 2026-06-30", data["hint"])
        self.assertIn("2026-08-15", data["hint"])

    def test_unlock(self):
        fs = 19405684991.0  # 平安银行流通股本(股)
        mr = self._fake(company_unlock=lambda code: [
            {"rdate": "2018-05-21", "ramount": 25224.8, "rprice": 27.2932,
             "batch": 15, "pdate": "2015-05-20"}])
        with mock.patch.object(market, "get_mr", return_value=mr), \
             mock.patch.object(market, "_fetch_instrument_meta", return_value={"float_shares": fs}):
            data = self.client.get("/api/cn/unlock?symbol=000001.SZ").get_json()
        row = data["rows"][0]
        self.assertEqual(row["解禁日期"], "2018-05-21")
        self.assertEqual(row["解禁数量(万股)"], 25224.8)
        self.assertEqual(row["批次"], 15)
        self.assertEqual(row["解禁市值(亿)"], 27.2932, "rprice 实测即解禁市值(亿元)")
        self.assertEqual(row["解禁均价(元)"], round(27.2932 * 1e4 / 25224.8, 2),
                         "单价 = 市值/数量 反推")
        self.assertEqual(row["占流通股%"], round(25224.8 * 1e4 / fs * 100, 2))

    def test_unlock_float_shares_fallback(self):
        # AF 元数据缺失 → 流通股本 = 麦蕊流通市值 / 最新价
        mr = self._fake(company_unlock=lambda code: [
            {"rdate": "2018-05-21", "ramount": 25224.8, "rprice": 27.2932, "batch": 15}])
        with mock.patch.object(market, "get_mr", return_value=mr), \
             mock.patch.object(market, "_fetch_instrument_meta", return_value=None), \
             mock.patch.object(market, "_mr_instrument", return_value={"float_value": 1.9405684991e10 * 10.0}), \
             mock.patch.object(market, "fetch_quote", return_value={"last_price": 10.0}):
            data = self.client.get("/api/cn/unlock?symbol=000001.SZ").get_json()
        self.assertEqual(data["rows"][0]["占流通股%"],
                         round(25224.8 * 1e4 / 19405684991.0 * 100, 2))

    def test_unlock_no_float_shares(self):
        mr = self._fake(company_unlock=lambda code: [
            {"rdate": "2018-05-21", "ramount": 25224.8, "rprice": 27.2932, "batch": 15}])
        with mock.patch.object(market, "get_mr", return_value=mr), \
             mock.patch.object(market, "_fetch_instrument_meta", return_value=None), \
             mock.patch.object(market, "_mr_instrument", return_value=None), \
             mock.patch.object(market, "fetch_quote", return_value=None):
            data = self.client.get("/api/cn/unlock?symbol=000001.SZ").get_json()
        self.assertIsNone(data["rows"][0]["占流通股%"], "无流通股本 → 占比留空")
        self.assertIsNotNone(data["rows"][0]["解禁市值(亿)"], "市值不依赖流通股本")
    def test_source_unavailable(self):
        mr = self._fake(company_unlock=mock.Mock(side_effect=RuntimeError("boom")))
        with mock.patch.object(market, "get_mr", return_value=mr):
            data = self.client.get("/api/cn/unlock?symbol=000001.SZ").get_json()
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["hint"], "数据源暂时不可用")

    def test_cache_hits_within_ttl(self):
        call = mock.Mock(return_value=[{"jzrq": "2026-06-30", "gdhs": 1, "bh": "-"}])
        mr = self._fake(company_holder_change=call)
        with mock.patch.object(market, "get_mr", return_value=mr):
            self.client.get("/api/cn/holder-change?symbol=000001.SZ")
            self.client.get("/api/cn/holder-change?symbol=000001.SZ")
        self.assertEqual(call.call_count, 1, "TTL 内应命中的数据层缓存")


if __name__ == "__main__":
    unittest.main(verbosity=2)
