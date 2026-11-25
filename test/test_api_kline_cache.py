# -*- coding: utf-8 -*-
"""/api/kline 缓存回归测试: 缓存条目不得携带 quote。

TTLCache 存引用, 若把挂了 quote 的响应存进缓存, 事后修改会污染缓存条目,
后续命中拿到最长 TTL 前的旧快照 (1w/1M 可达 300s)。契约: 缓存条目不含
quote, 每次返回前现挂新鲜快照 (fetch_quote 自身 30s 缓存)。

运行:
    venv/Scripts/python.exe -u visual/test/test_api_kline_cache.py
"""

import gzip
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import pandas as pd

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))


def _kline_df(n=30):
    idx = pd.date_range("2026-07-01", periods=n, freq="D")
    base = 10.0 + pd.Series(range(n), index=idx, dtype=float) * 0.01
    return pd.DataFrame({
        "open": base, "high": base + 0.2, "low": base - 0.2,
        "close": base + 0.1, "volume": 100000.0, "amount": 1_000_000.0,
    }, index=idx)


class KlineCacheTest(unittest.TestCase):
    SYMBOL = "600000.SH"
    COUNT = 200  # 1w 默认 count (MINUTE_COUNTS.get("1w", 200))

    @classmethod
    def setUpClass(cls):
        import trades
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_trades.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        if not any(u["username"] == "kline_admin" for u in trades.list_users()):
            trades.create_user("kline_admin", "password123", is_admin=True)
        from app import create_app
        cls.app = create_app()
        cls.client = cls.app.test_client()
        headers = cls._csrf(cls.client)
        r = cls.client.post(
            "/api/auth/login",
            data=json.dumps({"username": "kline_admin", "password": "password123"}),
            content_type="application/json", headers=headers,
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

    def setUp(self):
        from api import kline as kline_api
        from market import kline_cache, kline_cache_long, kline_cache_minute
        self.api = kline_api
        kline_cache.clear()
        kline_cache_long.clear()
        kline_cache_minute.clear()
        # 盘后 _attach_quote 只读本地快照不调 fetch_quote; 这里测的是盘中挂新鲜快照的契约
        live = mock.patch("market_hours.is_live", return_value=True)
        live.start()
        self.addCleanup(live.stop)

    def _patches(self, quotes=None, quote_exc=None):
        """mock fetch_kline_ex / fetch_quote / _is_index_symbol (不碰真实行情)。"""
        it = iter(quotes or [])

        def quote_side(*_a, **_k):
            try:
                return next(it)
            except StopIteration:
                return (quotes or [None])[-1]

        return (
            mock.patch.object(self.api, "fetch_kline_ex",
                              return_value=(_kline_df(), "浦发银行", "mairui")),
            mock.patch.object(self.api, "fetch_quote",
                              side_effect=quote_exc or quote_side),
            mock.patch.object(self.api, "_is_index_symbol", return_value=False),
            mock.patch.object(self.api, "peek_instrument_meta",
                              return_value={"float_shares": 1.0e9,
                                            "total_shares": 1.1e9,
                                            "type": "stock"}),
        )

    def _get(self, period="1w"):
        return self.client.get(f"/api/kline?symbol={self.SYMBOL}&period={period}")

    def test_cached_entry_has_no_quote_and_hit_gets_fresh(self):
        p1, p2, p3, p4 = self._patches(
            quotes=[{"last_price": 10.0, "prev_close": 9.9},
                    {"last_price": 11.0, "prev_close": 9.9}])
        with p1, p2, p3, p4:
            r1 = self._get()
            r2 = self._get()
        b1, b2 = r1.get_json(), r2.get_json()
        self.assertFalse(b1["meta"]["cached"])
        self.assertEqual(b1["quote"]["last_price"], 10.0)
        self.assertTrue(b2["meta"]["cached"])
        # 回归点: 缓存条目本身不含 quote; 命中时现挂新快照而不是首建时的旧值
        cached = self.api.kline_cache_long.get(f"{self.SYMBOL}:1w:{self.COUNT}:qfq")
        self.assertIsNotNone(cached)
        self.assertNotIn("quote", cached)
        self.assertEqual(b2["quote"]["last_price"], 11.0)

    def test_hfq_fallback_does_not_mask_recovered_source(self):
        df = _kline_df()
        url = f"/api/kline?symbol={self.SYMBOL}&period=1w&adjust=hfq&format=rows"
        source_responses = [(None, None, None), (df.copy(), "test", "mock"), (df, "test", "mock")]
        with mock.patch.object(self.api, "fetch_kline_ex", side_effect=source_responses) as fetch, \
             mock.patch.object(self.api, "_attach_quote"), mock.patch.object(self.api, "_is_etf", return_value=False):
            first = self.client.get(url).get_json()["meta"]
            second = self.client.get(url).get_json()["meta"]
            third = self.client.get(url).get_json()["meta"]
        self.assertEqual(first["adjust"], "forward")
        self.assertTrue(first["adjust_fallback"])
        self.assertEqual(second["adjust"], "hfq")
        self.assertFalse(second["adjust_fallback"])
        self.assertTrue(third["cached"])
        self.assertEqual([call.kwargs["adjust"] for call in fetch.call_args_list], ["hfq", "forward", "hfq"])

    def test_quote_failure_still_serves_kline(self):
        p1, p2, p3, p4 = self._patches(quote_exc=RuntimeError("行情源抖动"))
        with p1, p2, p3, p4:
            r1 = self._get()
            r2 = self._get()
        b1, b2 = r1.get_json(), r2.get_json()
        self.assertNotIn("quote", b1)
        self.assertTrue(b2["meta"]["cached"])
        self.assertNotIn("quote", b2)
        self.assertEqual(len(b2["klines"]), 30)

    def test_server_timing_includes_final_quote_and_json_on_both_cache_paths(self):
        p1, p2, p3, p4 = self._patches()
        with p1, p2, p3, p4, mock.patch.object(self.api.perf, "ENABLED", False):
            cold = self._get()
            warm = self._get()
        for response in (cold, warm):
            self.assertEqual(response.status_code, 200)
            header = response.headers["Server-Timing"]
            for phase in ("app", "attach_quote", "json"):
                self.assertRegex(header, rf"(?:^|, ){phase};dur=\d+(?:\.\d+)?")
        self.assertIn("indicators;dur=", cold.headers["Server-Timing"])
        self.assertNotIn("indicators;dur=", warm.headers["Server-Timing"])

    def test_large_chart_json_negotiates_gzip(self):
        path = f"/api/kline?symbol={self.SYMBOL}&period=1w&format=rows&limit=30"
        p1, p2, p3, p4 = self._patches()
        with p1, p2, p3, p4, mock.patch.object(self.api, "_session_meta", return_value={}):
            self.client.get(path)  # compare two cache hits with identical metadata
            plain = self.client.get(path)
            zipped = self.client.get(path, headers={"Accept-Encoding": "gzip, br"})
            refused = self.client.get(path, headers={"Accept-Encoding": "gzip;q=0, *;q=1"})
        self.assertEqual(zipped.headers["Content-Encoding"], "gzip")
        self.assertIn("Accept-Encoding", zipped.vary)
        self.assertIn("Accept-Encoding", plain.vary)
        self.assertEqual(int(zipped.headers["Content-Length"]), len(zipped.data))
        self.assertLess(len(zipped.data), len(plain.data))
        self.assertEqual(gzip.decompress(zipped.data), plain.data)
        self.assertNotIn("Content-Encoding", refused.headers)
        self.assertEqual(refused.data, plain.data)

    def test_chart_json_prefers_zstd_and_respects_client_preferences(self):
        try:
            from compression.zstd import decompress
        except ImportError:
            from zstandard import decompress

        path = f"/api/kline?symbol={self.SYMBOL}&period=1w&format=rows&limit=30"
        p1, p2, p3, p4 = self._patches()
        with p1, p2, p3, p4, mock.patch.object(self.api, "_session_meta", return_value={}):
            self.client.get(path)
            plain = self.client.get(path)
            cases = (
                ("gzip, zstd", "zstd", decompress),
                ("ZSTD, gzip", "zstd", decompress),
                ("zstd;q=1, gzip;q=0.5", "zstd", decompress),
                ("gzip;q=1, zstd;q=0.5", "gzip", gzip.decompress),
                ("zstd;q=0, gzip", "gzip", gzip.decompress),
                ("zstd;q=0.5, gzip;q=0.5, identity;q=1", None, lambda body: body),
            )
            for accept, encoding, decode in cases:
                with self.subTest(accept=accept):
                    response = self.client.get(path, headers={"Accept-Encoding": accept})
                    self.assertEqual(response.headers.get("Content-Encoding"), encoding)
                    self.assertIn("Accept-Encoding", response.vary)
                    self.assertEqual(response.content_length, len(response.data))
                    self.assertEqual(decode(response.data), plain.data)
            with mock.patch("app._compress_zstd", None):
                response = self.client.get(path, headers={"Accept-Encoding": "gzip, zstd"})
        self.assertEqual(response.headers["Content-Encoding"], "gzip")
        self.assertEqual(gzip.decompress(response.data), plain.data)

    def test_proxy_mode_leaves_json_for_nginx(self):
        import trades
        from app import create_app

        with mock.patch.dict(os.environ, {"VISUAL_HTTP_COMPRESSION": "proxy"}):
            client = create_app().test_client()
        token, _ = trades.create_session(trades.list_users()[0]["id"])
        client.set_cookie("session", token)
        path = f"/api/kline?symbol={self.SYMBOL}&period=1w&format=rows&limit=30"
        p1, p2, p3, p4 = self._patches()
        with p1, p2, p3, p4:
            response = client.get(path, headers={"Accept-Encoding": "zstd, gzip"})
        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.content_length, 1024)
        self.assertNotIn("Content-Encoding", response.headers)
        self.assertEqual(response.get_json()["count"], 30)

    def test_chips_endpoint_returns_payload(self):
        payload = {"buckets": [{"price": 10.0, "weight": 1.0}], "avgCost": 10.0}
        with mock.patch.object(self.api, "get_chips", return_value=payload) as gc, \
             mock.patch.object(self.api, "_is_etf", return_value=False):
            r = self.client.get(f"/api/chips?symbol={self.SYMBOL}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["chips"]["avgCost"], 10.0)
        gc.assert_called_once()

    def test_chips_endpoint_etf_returns_data(self):
        """ETF 走与股票相同的路径 (不再拦截); 指数才拦截。"""
        payload = {"buckets": [{"price": 4.0, "weight": 1.0}], "avgCost": 4.0}
        with mock.patch.object(self.api, "_is_etf", return_value=True), \
             mock.patch.object(self.api, "_is_index_symbol", return_value=False), \
             mock.patch.object(self.api, "get_chips", return_value=payload) as gc:
            r = self.client.get(f"/api/chips?symbol={self.SYMBOL}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["chips"]["avgCost"], 4.0)
        gc.assert_called_once_with(self.SYMBOL, "1d")

    def test_chips_endpoint_index_returns_null(self):
        with mock.patch.object(self.api, "_is_index_symbol", return_value=True), \
             mock.patch.object(self.api, "get_chips") as gc:
            r = self.client.get(f"/api/chips?symbol={self.SYMBOL}")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.get_json()["chips"])
        gc.assert_not_called()

    def test_chips_endpoint_forwards_period(self):
        """period 透传给 get_chips (周/月K 用更长日线窗口)。"""
        with mock.patch.object(self.api, "get_chips", return_value=None) as gc, \
             mock.patch.object(self.api, "_is_etf", return_value=False):
            r = self.client.get(f"/api/chips?symbol={self.SYMBOL}&period=1w")
        self.assertEqual(r.status_code, 200)
        gc.assert_called_once_with(self.SYMBOL, "1w")

    def test_chips_endpoint_invalid_period_falls_back_daily(self):
        with mock.patch.object(self.api, "get_chips", return_value=None) as gc, \
             mock.patch.object(self.api, "_is_etf", return_value=False):
            self.client.get(f"/api/chips?symbol={self.SYMBOL}&period=5m")
        gc.assert_called_once_with(self.SYMBOL, "1d")

    def test_depth_endpoint(self):
        from api import market as market_api
        payload = {"symbol": self.SYMBOL, "bid_prices": [1.0], "ask_prices": [2.0]}
        with mock.patch.object(market_api, "fetch_depth", return_value=payload) as fd:
            r = self.client.get(f"/api/depth?symbol={self.SYMBOL}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["depth"]["bid_prices"], [1.0])
        fd.assert_called_once()

    def test_depth_endpoint_missing_symbol(self):
        r = self.client.get("/api/depth")
        self.assertEqual(r.status_code, 400)

    def test_kline_includes_obv_vol_ma_and_meta(self):
        p1, p2, p3, p4 = self._patches(quotes=[{"last_price": 10.0}])
        with p1, p2, p3, p4:
            r = self._get()
        b = r.get_json()
        self.assertEqual(b["float_shares"], 1.0e9)
        self.assertEqual(b["instrument_type"], "stock")
        self.assertEqual(b["obv_params"], {"ma_period": 30})
        last = b["klines"][-1]
        for key in ("obv", "maobv", "vol_ma5", "vol_ma10", "vol_ma20"):
            self.assertIn(key, last)


    def test_off_session_quote_from_local_snapshot_without_fetch(self):
        """盘后挂收盘快照 (长 TTL) 但不发网络: 1.25s 的 quote_cache 早过期, 不能因此丢 quote。"""
        p1, p2, p3, p4 = self._patches(quotes=[{"last_price": 99.0}])
        snap = {"last_price": 10.5, "prev_close": 10.0}
        with p1, p2 as fq, p3, p4, \
                mock.patch("market_hours.is_live", return_value=False), \
                mock.patch.object(self.api, "_quote_cache_lookup", return_value=snap) as lk:
            b = self._get().get_json()
        fq.assert_not_called()
        lk.assert_called_with(self.SYMBOL, True)
        self.assertEqual(b["quote"]["last_price"], 10.5)

    def test_etf_kline_defers_premium(self):
        """ETF 溢价不再内联在主路径: 只预热 + meta.deferred 声明, bar 不带 premium。

        溢价依赖 akshare 净值 (无缓存无超时), 内联会让每次 ETF 切换都等它。
        """
        p1, p2, p3, p4 = self._patches(quotes=[{"last_price": 10.0}])
        with p1, p2, p3, p4, \
                mock.patch.object(self.api, "_is_etf", return_value=True), \
                mock.patch.object(self.api.premium, "request") as req:
            r = self._get()
        b = r.get_json()
        self.assertTrue(b["is_etf"])
        self.assertEqual(b["meta"]["deferred"], ["premium"])
        req.assert_called_once()
        self.assertNotIn("premium", b["klines"][-1])

    def test_non_etf_kline_has_no_deferred(self):
        p1, p2, p3, p4 = self._patches(quotes=[{"last_price": 10.0}])
        with p1, p2, p3, p4, \
                mock.patch.object(self.api, "_is_etf", return_value=False), \
                mock.patch.object(self.api.premium, "request") as req:
            r = self._get()
        self.assertEqual(r.get_json()["meta"]["deferred"], [])
        req.assert_not_called()

    def test_deferred_endpoint_returns_ready_payload(self):
        ready = {"ready": True, "dates": ["2026-09-11"], "values": [1.25],
                 "params": {"source": "akshare"}}
        with mock.patch.object(self.api.premium, "get", return_value=ready) as pg:
            r = self.client.get(
                f"/api/kline/deferred?symbol={self.SYMBOL}&period=1d&fields=premium")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["premium"]["ready"])
        self.assertEqual(r.get_json()["premium"]["values"], [1.25])
        pg.assert_called_once()

    def test_deferred_endpoint_not_ready_is_200(self):
        """未就绪用标志表达而非错误码: 前端退避重试, 不弹错误提示。"""
        with mock.patch.object(self.api.premium, "get", return_value={"ready": False}):
            r = self.client.get(f"/api/kline/deferred?symbol={self.SYMBOL}&period=1d")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()["premium"]["ready"])

    def test_deferred_endpoint_rejects_minute_period(self):
        r = self.client.get(f"/api/kline/deferred?symbol={self.SYMBOL}&period=5m")
        self.assertEqual(r.status_code, 400)

    def test_deferred_endpoint_missing_symbol(self):
        r = self.client.get("/api/kline/deferred")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
