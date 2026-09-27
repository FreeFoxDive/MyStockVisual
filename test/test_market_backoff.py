# -*- coding: utf-8 -*-
"""market.py 麦蕊 429 熔断退避单测 (离线, 全 mock)。

覆盖:
- _mr_is_429 只认状态码 (000429.SZ 这类代码不误伤)
- SDK 调用命中 429 → 开退避窗口, 窗口内不再发 HTTP (抛 MairuiBackoff)
- 非 429 错误不误开窗口
- 直接 HTTP 接口 (_mr_urlopen_json) 同样受退避约束, 并识别 Retry-After

运行:
    venv/Scripts/python.exe -u visual/test/test_market_backoff.py
"""
from __future__ import annotations

import io
import os
import sys
import unittest
import urllib.error
from unittest import mock

_VISUAL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _VISUAL_DIR not in sys.path:
    sys.path.insert(0, _VISUAL_DIR)

import market  # noqa: E402


class _FakeMairuiHTTPError(Exception):
    """模拟 mairui.MairuiHTTPError (带 status_code / payload)。"""

    def __init__(self, status_code=None, payload=None, msg=None):
        super().__init__(msg or f"HTTP {status_code}: https://api.mairuiapi.com/hsindex/list/KEY")
        self.status_code = status_code
        self.payload = payload


class BackoffBase(unittest.TestCase):
    def setUp(self):
        self._orig = market._mr_backoff_until
        market._mr_backoff_until = 0.0
        self.addCleanup(lambda: setattr(market, "_mr_backoff_until", self._orig))
        # 429 退避会推告警: 不打桩就真发钉钉/ntfy (单个文件单跑时这是唯一兜底)。
        # 要验证"确实通知了"的用例在自己的 with 里再 patch 一层。
        p = mock.patch.object(market.source_alert, "notify", return_value=True)
        p.start()
        self.addCleanup(p.stop)


class Is429Test(BackoffBase):
    def test_429_by_status_code(self):
        self.assertTrue(market._mr_is_429(_FakeMairuiHTTPError(429)))

    def test_code_containing_429_not_misjudged(self):
        # 000429.SZ 是深市股票代码, 不能因为含 "429" 就当成限流
        self.assertFalse(market._mr_is_429(
            _FakeMairuiHTTPError(404, msg="HTTP 404: .../hsstock/history/000429.SZ/d/n/KEY")
        ))
        self.assertFalse(market._mr_is_429(RuntimeError("000429 无数据")))

    def test_urllib_http_error(self):
        e = urllib.error.HTTPError(
            "https://api.mairuiapi.com/x", 429, "Too Many Requests", {}, io.BytesIO(b"")
        )
        self.assertTrue(market._mr_is_429(e))


class ClientBackoffTest(BackoffBase):
    def test_429_opens_window_and_short_circuits(self):
        calls = []

        class _Client:
            def index_list(self):
                calls.append(1)
                raise _FakeMairuiHTTPError(429, payload={"code": 103})

        c = market._MairuiClient(_Client())
        with self.assertRaises(_FakeMairuiHTTPError):
            c.index_list()
        self.assertGreater(market._mr_backoff_remaining(), 0)
        # 窗口内不再发 HTTP → 直接抛 MairuiBackoff
        with self.assertRaises(market.MairuiBackoff):
            c.index_list()
        self.assertEqual(len(calls), 1)

    def test_success_passthrough(self):
        class _Client:
            def roe(self):
                return [{"dm": "000001"}]

        self.assertEqual(market._MairuiClient(_Client()).roe(), [{"dm": "000001"}])

    def test_non_429_does_not_open_window(self):
        class _Client:
            def index_list(self):
                raise _FakeMairuiHTTPError(404)

        c = market._MairuiClient(_Client())
        with self.assertRaises(_FakeMairuiHTTPError):
            c.index_list()
        self.assertEqual(market._mr_backoff_remaining(), 0.0)


class BackoffAlertTest(BackoffBase):
    """429 退避要推一条告警, 且**每个源每天最多一条** (source_alert 的当日闸门)。"""

    def setUp(self):
        super().setUp()
        import source_alert
        source_alert.reset()
        self.sent = []
        self._p = mock.patch.object(
            market.source_alert, "notify",
            side_effect=lambda src, reason, **kw: (
                self.sent.append((src, reason, kw)), True)[1])
        self._p.start()
        self.addCleanup(self._p.stop)
        self.addCleanup(source_alert.reset)

    def test_sdk_429_notifies_once_with_safe_detail(self):
        class _Client:
            def stock_history(self, symbol, period, div):
                raise _FakeMairuiHTTPError(429, payload={"code": 103})

        c = market._MairuiClient(_Client())
        for _ in range(3):                      # 窗口内的后续调用不会再发 HTTP
            with self.assertRaises(Exception):
                c.stock_history("600519.SH", "d", "n")
        self.assertEqual(len(self.sent), 1, "同一轮限流只通报一次")
        src, reason, kw = self.sent[0]
        self.assertEqual(src, "mairui")
        self.assertIn("429", reason)
        self.assertEqual(kw.get("detail"), "stock_history() 600519.SH")

    def test_direct_http_429_detail_drops_the_licence_segment(self):
        url = "https://api.mairuiapi.com/hszbl/fsjy/600519.SH/3f2504e0-4f89-11d3-9a0c-0305e82c3301"
        err = urllib.error.HTTPError(url, 429, "Too Many Requests", {}, io.BytesIO(b""))
        with mock.patch.object(market.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                market._mr_urlopen_json(url)
        _src, _reason, kw = self.sent[0]
        self.assertEqual(kw.get("detail"),
                         "https://api.mairuiapi.com/hszbl/fsjy/600519.SH")
        self.assertNotIn("3f2504e0", str(kw.get("detail")), "证书 key 不许进通知")

    def test_window_skip_does_not_notify(self):
        """退避窗口内的主动跳过 (未发 HTTP) 不是新事件, 不该再报一次。"""
        market._mr_backoff_until = market.time.time() + 60
        with self.assertRaises(market.MairuiBackoff):
            market._mr_urlopen_json("https://api.mairuiapi.com/jj/lskx/510300/d/KEY")
        self.assertEqual(self.sent, [])


class DirectHttpBackoffTest(BackoffBase):
    URL = "https://api.mairuiapi.com/jj/lskx/510300/d/KEY"

    def test_429_honours_retry_after(self):
        err = urllib.error.HTTPError(
            self.URL, 429, "Too Many Requests", {"Retry-After": "30"}, io.BytesIO(b"")
        )
        with mock.patch.object(market.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(urllib.error.HTTPError):
                market._mr_urlopen_json(self.URL)
        self.assertGreaterEqual(market._mr_backoff_remaining(), 25.0)

    def test_window_skips_http(self):
        market._mr_backoff_until = market.time.time() + 60
        with mock.patch.object(market.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(market.MairuiBackoff):
                market._mr_urlopen_json(self.URL)
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
