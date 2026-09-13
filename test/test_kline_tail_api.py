# -*- coding: utf-8 -*-
"""GET /api/kline/tail 路由测试: 鉴权/参数/短TTL/与 /api/kline 末根一致。

契约核心: 前端只渲染后端权威 bar。tail 与 /api/kline 必须同口径 —— 同一
fetch_kline_ex + compute_all_indicators 下, 末根逐字段相等; 任何口径分叉都要
被这条测试拦住。

运行:
    venv/Scripts/python.exe -u visual/test/test_kline_tail_api.py
"""
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


def _kline_df(n=40, end="2026-09-11", close=34.64):
    idx = pd.bdate_range(end=end, periods=n)
    base = [close - (n - 1 - i) * 0.1 for i in range(n)]
    return pd.DataFrame(
        {
            "open": base,
            "high": [c + 0.5 for c in base],
            "low": [c - 0.5 for c in base],
            "close": base,
            "volume": [1000 + i for i in range(n)],
            "amount": [100000.0 + i for i in range(n)],
        },
        index=idx,
    )


class KlineTailRouteTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_tail.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        trades.create_user("tail_u", "password123")
        cls.uid = trades.list_users()[0]["id"]
        from app import create_app
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    def setUp(self):
        _reset_rate_limit()
        from api import kline as kline_mod
        self.kline_mod = kline_mod
        kline_mod._tail_cache.clear()
        kline_mod.kline_cache._cache.clear()
        self.client = self.app.test_client()
        token, _ = trades.create_session(self.uid)
        self.client.set_cookie("session", token)
        _csrf(self.client)

    def _mock_upstream(self, df=None):
        df = _kline_df() if df is None else df
        return (
            mock.patch.object(self.kline_mod, "fetch_kline_ex",
                              return_value=(df, "双环传动", "alphafeed")),
            mock.patch.object(self.kline_mod, "fetch_quote", return_value=None),
            mock.patch.object(self.kline_mod, "_fetch_instrument_meta", return_value=None),
        )

    def test_requires_login(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get(f"/api/kline/tail?symbol={SYM}").status_code, 401)

    def test_missing_symbol(self):
        self.assertEqual(self.client.get("/api/kline/tail").status_code, 400)

    def test_reject_minute_period(self):
        r = self.client.get(f"/api/kline/tail?symbol={SYM}&period=5m")
        self.assertEqual(r.status_code, 400)

    def test_returns_tail_bars_with_indicators(self):
        m1, m2, m3 = self._mock_upstream()
        with m1, m2, m3:
            body = self.client.get(f"/api/kline/tail?symbol={SYM}&n=2").get_json()
        self.assertEqual(body["symbol"], SYM)
        self.assertEqual(len(body["bars"]), 2)
        last = body["bars"][-1]
        self.assertEqual(last["date"], "2026-09-11")
        self.assertIn("ma5", last)
        self.assertIn("macd_dif", last)
        for key in ("is_trading_day", "session_phase", "server_time", "last_trade_date", "source"):
            self.assertIn(key, body["meta"])

    def test_tail_last_bar_matches_kline_last_bar(self):
        """契约: 同一份 df 下 tail 末根与 /api/kline 末根逐字段相等。"""
        m1, m2, m3 = self._mock_upstream()
        with m1, m2, m3:
            tail = self.client.get(f"/api/kline/tail?symbol={SYM}&n=1").get_json()
            full = self.client.get(f"/api/kline?symbol={SYM}&period=1d&count=1006").get_json()
        self.assertEqual(tail["bars"][-1], full["klines"][-1],
                         "tail 与 /api/kline 的末根必须逐字段一致")

    def test_short_ttl_merges_repeated_calls(self):
        m1, m2, m3 = self._mock_upstream()
        calls = []
        original = self.kline_mod.fetch_kline_ex

        def _count(*a, **kw):
            calls.append(1)
            return original(*a, **kw)

        with mock.patch.object(self.kline_mod, "fetch_kline_ex", side_effect=_count), m2, m3:
            self.client.get(f"/api/kline/tail?symbol={SYM}&n=1")
            self.client.get(f"/api/kline/tail?symbol={SYM}&n=1")
        self.assertEqual(len(calls), 1, "TTL 内重复请求应命中短缓存, 只取数一次")

    def test_404_when_no_data(self):
        with mock.patch.object(self.kline_mod, "fetch_kline_ex", return_value=(None, None, None)):
            r = self.client.get(f"/api/kline/tail?symbol={SYM}")
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
