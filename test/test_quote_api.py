# -*- coding: utf-8 -*-
"""GET /api/quote 路由测试: 鉴权 / 缺参 / 快照附带交易日标志。

非交易日前端若见末根日K日期早于今天, 会用快照补一根当日 bar; 而快照在周末
仍是上一交易日的残留值 (volume>0), 补出来那根 OHLC 全是旧值。故快照必须带
is_trading_day 供前端拦截; 该字段不得写回 fetch_quote 的共享缓存条目。

运行:
    venv/Scripts/python.exe -u visual/test/test_quote_api.py
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import security  # noqa: E402
import trades  # noqa: E402

SYM = "002472.SZ"


def _reset_rate_limit():
    security._rate_limit_tokens = float(security.RATE_LIMIT_PER_MIN)
    security._rate_limit_last_refill = time.time()


def _csrf(client):
    client.get("/login.html")
    headers = {}
    c = client.get_cookie("csrf_token")
    if c is not None:
        headers["X-CSRF-Token"] = c.value if hasattr(c, "value") else str(c)
    return headers


class QuoteRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_quote.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        trades.create_user("quote_u", "password123")
        cls.uid = trades.list_users()[0]["id"]
        from app import create_app
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    def setUp(self):
        _reset_rate_limit()
        self.client = self.app.test_client()
        token, _ = trades.create_session(self.uid)
        self.client.set_cookie("session", token)
        _csrf(self.client)

    def test_requires_login(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get(f"/api/quote?symbol={SYM}").status_code, 401)

    def test_missing_symbol(self):
        self.assertEqual(self.client.get("/api/quote").status_code, 400)

    def test_non_trading_day_flag_and_cache_not_mutated(self):
        q = {"symbol": SYM, "last_price": 34.64, "prev_close": 36.27,
             "open": 36.16, "high": 36.19, "low": 34.13, "volume": 280705}
        with mock.patch("api.market.fetch_quote", return_value=q), \
                mock.patch("api.market.market_hours.is_trading_day", return_value=False):
            resp = self.client.get(f"/api/quote?symbol={SYM}")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIs(body["is_trading_day"], False)
        self.assertEqual(body["last_price"], 34.64)
        self.assertNotIn("is_trading_day", q, "不得原地改写 fetch_quote 的返回条目")

    def test_trading_day_flag_true(self):
        q = {"symbol": SYM, "last_price": 34.64, "volume": 280705}
        with mock.patch("api.market.fetch_quote", return_value=q), \
                mock.patch("api.market.market_hours.is_trading_day", return_value=True):
            body = self.client.get(f"/api/quote?symbol={SYM}").get_json()
        self.assertIs(body["is_trading_day"], True)

    def test_quotes_batch_carries_flag_without_mutating(self):
        q = {"symbol": SYM, "last_price": 34.64, "volume": 280705}
        with mock.patch("api.market.fetch_quotes", return_value={SYM: q}), \
                mock.patch("api.market.market_hours.is_trading_day", return_value=False):
            body = self.client.get(f"/api/quotes?symbols={SYM}").get_json()
        self.assertIs(body[SYM]["is_trading_day"], False)
        self.assertEqual(body[SYM]["last_price"], 34.64)
        self.assertNotIn("is_trading_day", q, "不得原地改写 fetch_quotes 的返回条目")


if __name__ == "__main__":
    unittest.main()
