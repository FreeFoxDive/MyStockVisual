# -*- coding: utf-8 -*-
"""快照 SSE (/api/stream/quotes) 测试: 帧协议 / 参数与上限 / 保活 / 并发槽位归还。

interval 置 0 且只读前 2 帧即关闭, 避免长连接拖住用例; fetch_quotes 全 mock, 不联网。

运行:
    venv/Scripts/python.exe -u visual/test/test_quote_sse.py
"""

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

_VISUAL_DIR = Path(__file__).resolve().parents[1]
if str(_VISUAL_DIR) not in sys.path:
    sys.path.insert(0, str(_VISUAL_DIR))

import trades  # noqa: E402

SYM = "600000.SH"


class QuoteSseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls._db = Path(cls._tmpdir.name) / "test_sse.db"
        cls._orig_db = trades._db_path
        trades.init_db(cls._db)
        trades.create_user("sse_u", "password123")
        cls.uid = trades.list_users()[0]["id"]
        from app import create_app
        cls.app = create_app()

    @classmethod
    def tearDownClass(cls):
        trades._db_path = cls._orig_db
        cls._tmpdir.cleanup()

    def setUp(self):
        from api import stream
        self.stream = stream
        self._orig_slots = stream._sse_slots
        self._orig_interval = stream.QUOTE_SSE_INTERVAL
        self._orig_tick = stream.QUOTE_SSE_TICK
        self._orig_lifetime = stream.QUOTE_SSE_MAX_LIFETIME
        stream._sse_slots = threading.BoundedSemaphore(stream.QUOTE_SSE_MAX_CLIENTS)
        stream.QUOTE_SSE_INTERVAL = 0.0
        stream.QUOTE_SSE_TICK = 0.0  # 测试内不真实等待, 断连后立即归还槽位
        stream.QUOTE_SSE_MAX_LIFETIME = 3600.0  # 默认不在用例内到期
        self.addCleanup(self._restore)
        # 建流门控 (can_connect_stream) 与取数门控 (is_live) 都必须固定住: 它们各自
        # 会去问日历/当前时间, 不 patch 的话用例会随真实墙钟变红 (09:15-09:30 或
        # 盘外跑就换一套行为)。
        self._in_session = mock.patch.object(stream.market_hours, "in_session", return_value=True)
        self._in_session.start()
        self.addCleanup(self._in_session.stop)
        self._phase = mock.patch.object(stream.market_hours, "session_phase", return_value="trading")
        self._phase.start()
        self.addCleanup(self._phase.stop)
        self._live = mock.patch.object(stream.market_hours, "is_live", return_value=True)
        self._live.start()
        self.addCleanup(self._live.stop)
        self._stream_ok = mock.patch.object(stream.market_hours, "can_connect_stream", return_value=True)
        self._stream_ok.start()
        self.addCleanup(self._stream_ok.stop)
        self.client = self.app.test_client()
        token, _ = trades.create_session(self.uid)
        self.client.set_cookie("session", token)

    def _restore(self):
        self.stream._sse_slots = self._orig_slots
        self.stream.QUOTE_SSE_INTERVAL = self._orig_interval
        self.stream.QUOTE_SSE_TICK = self._orig_tick
        self.stream.QUOTE_SSE_MAX_LIFETIME = self._orig_lifetime

    def _frames(self, url, n=2):
        """读前 n 帧后立即关闭 (不触发无限循环)。"""
        resp = self.client.get(url, buffered=False)
        if resp.status_code != 200:
            resp.close()
            return resp, []
        out = []
        try:
            for chunk in resp.iter_encoded():
                out.append(chunk.decode("utf-8"))
                if len(out) >= n:
                    break
        finally:
            resp.close()
        return resp, out

    # 握手帧: retry + 相位帧 (event: market)。数据类断言一律跳过后再取,
    # 免得每加一个握手帧就要改一遍下标。
    def _frames_data(self, url, n=1):
        resp, frames = self._frames(url, n=n + 2)
        self.assertEqual(frames[0], "retry: 5000\n\n")
        self.assertTrue(frames[1].startswith("event: market\ndata: "), "首帧应报相位")
        return resp, frames[2:]

    def test_requires_login(self):
        anon = self.app.test_client()
        r = anon.get(f"/api/stream/quotes?symbols={SYM}")
        self.assertEqual(r.status_code, 401)
        r.close()

    def test_missing_symbols(self):
        r = self.client.get("/api/stream/quotes")
        self.assertEqual(r.status_code, 400)
        r.close()

    def test_frame_protocol_and_headers(self):
        quotes = {SYM: {"last_price": np.float64(10.5), "volume": np.int64(100)}}
        with mock.patch.object(self.stream.market, "fetch_quotes", return_value=quotes):
            resp, frames = self._frames(f"/api/stream/quotes?symbols={SYM}", n=3)
        self.assertEqual(frames[0], "retry: 5000\n\n")
        self.assertTrue(frames[1].startswith("event: market\ndata: "))
        self.assertTrue(frames[2].startswith("data: "))
        self.assertTrue(frames[2].endswith("\n\n"))
        payload = json.loads(frames[2][len("data: "):])
        self.assertEqual(payload[SYM]["last_price"], 10.5, "numpy 标量应被 NumpyEncoder 正常编码")
        self.assertEqual(payload[SYM]["volume"], 100)
        self.assertEqual(resp.mimetype, "text/event-stream")
        self.assertEqual(resp.headers["Cache-Control"], "no-cache")
        self.assertEqual(resp.headers["X-Accel-Buffering"], "no")

    def test_market_frame_carries_phase_and_next_wake_hint(self):
        """相位帧是前端唯一状态来源: 连上就知道现在是什么时段、下一次几点开始。"""
        with mock.patch.object(self.stream.market, "fetch_quotes", return_value={}):
            _resp, frames = self._frames(f"/api/stream/quotes?symbols={SYM}", n=2)
        payload = json.loads(frames[1][len("event: market\ndata: "):])
        for key in ("session_phase", "quote_live", "in_session", "is_trading_day",
                    "next_open_at", "next_open_in_sec", "next_live_at", "next_live_in_sec",
                    "stream_allowed", "calendar_source"):
            self.assertIn(key, payload, f"相位帧应带 {key} (与 /api/ping 同一份口径)")
        self.assertEqual(payload["session_phase"], "trading")

    def test_market_frame_only_on_phase_change(self):
        """相位不变不重复推: 它是事件, 不是心跳。"""
        with mock.patch.object(self.stream.market, "fetch_quotes", return_value={}):
            _resp, frames = self._frames(f"/api/stream/quotes?symbols={SYM}", n=6)
        markets = [f for f in frames if f.startswith("event: market")]
        self.assertEqual(len(markets), 1, "相位没变就不该重复推")
        self.assertEqual(frames[1], markets[0], "首帧即报相位")

    def test_market_frame_on_phase_change(self):
        """相位一变立刻推 —— 开盘/午休/收盘的切换不再靠前端轮询发现。"""
        calls = {"n": 0}

        def phase(*_a, **_k):
            calls["n"] += 1
            return "auction" if calls["n"] > 3 else "trading"

        with mock.patch.object(self.stream.market_hours, "session_phase", side_effect=phase), \
                mock.patch.object(self.stream.market, "fetch_quotes", return_value={}):
            _resp, frames = self._frames(f"/api/stream/quotes?symbols={SYM}", n=8)
        markets = [f for f in frames if f.startswith("event: market")]
        self.assertGreaterEqual(len(markets), 2, "相位变化要推新帧")
        head = len("event: market\ndata: ")
        self.assertEqual(json.loads(markets[0][head:])["session_phase"], "trading")
        self.assertEqual(json.loads(markets[-1][head:])["session_phase"], "auction")

    def test_close_frame_pushed_even_after_stream_gate_closes(self):
        """收盘那一帧必须照推: 建流门控只关"新连接", 不能拦既有连接上的推送。

        客户端拆流的**依据**就是这帧 (stream_allowed=false); 推送若被建流门控挡住,
        "已收盘"就永远不会到达, 只能等 10 分钟兜底心跳 —— 实测会晚 600s。
        """
        calls = {"n": 0}

        def phase(*_a, **_k):
            calls["n"] += 1
            return "closed" if calls["n"] > 3 else "trading"

        gate = {"n": 0}

        def connectable(*_a, **_k):
            # 建立时窗口还开着, 建好之后窗口关闭 (15:01 的真实顺序)
            gate["n"] += 1
            return gate["n"] <= 1

        with mock.patch.object(self.stream.market_hours, "session_phase", side_effect=phase), \
                mock.patch.object(self.stream.market_hours, "can_connect_stream", side_effect=connectable), \
                mock.patch.object(self.stream.market, "fetch_quotes", return_value={}):
            resp, frames = self._frames(f"/api/stream/quotes?symbols={SYM}", n=6)
        self.assertNotEqual(resp.status_code, 425, "建立时窗口还开着, 不该被拒")
        markets = [f for f in frames if f.startswith("event: market")]
        self.assertGreaterEqual(len(markets), 2, "既有连接上收盘帧仍要推出去")
        self.assertEqual(json.loads(markets[-1][len("event: market\ndata: "):])["session_phase"],
                         "closed")
        self.assertIs(json.loads(markets[-1][len("event: market\ndata: "):])["stream_allowed"],
                      False, "帧里要告诉前端窗口已关, 由前端据此拆流")

    def test_frame_carries_is_trading_day_flag(self):
        # 非交易日标志必须随快照下发, 前端据此拦截 "残留快照补当日 bar"
        quotes = {SYM: {"last_price": 10.5, "volume": 100}}
        with mock.patch.object(self.stream.market, "fetch_quotes", return_value=quotes), \
                mock.patch.object(self.stream.market_hours, "is_trading_day", return_value=False):
            _resp, frames = self._frames_data(f"/api/stream/quotes?symbols={SYM}")
        payload = json.loads(frames[0][len("data: "):])
        self.assertIs(payload[SYM]["is_trading_day"], False)
        self.assertEqual(payload[SYM]["last_price"], 10.5, "注入标志不得丢失原字段")
        self.assertNotIn("is_trading_day", quotes[SYM], "不得原地改写 fetch_quotes 的返回条目")

    def test_depth_named_event_and_cleanup(self):
        depth = {"symbol": SYM, "bid_prices": [10], "ask_prices": [11], "_revision": 1}
        with mock.patch.object(self.stream.market, "fetch_quotes", return_value={}), \
             mock.patch.object(self.stream.market, "fetch_depth", return_value=depth):
            _, frames = self._frames_data(f"/api/stream/quotes?symbols={SYM}&depth=1", n=2)
        self.assertTrue(frames[1].startswith("event: depth\ndata: "))
        self.assertFalse(self.stream.market._quote_interests)

    def test_indicators_named_event(self):
        payload = {"symbol": SYM, "bars": [{"date": "2026-09-14", "close": 10}], "_revision": 2}
        with mock.patch.object(self.stream.market, "fetch_quotes", return_value={}), \
             mock.patch("api.kline.build_kline_tail", return_value=payload) as build:
            _, frames = self._frames_data(
                f"/api/stream/quotes?symbols={SYM}&tail=1d&count=1006&adjust=none", n=2)
        self.assertTrue(frames[1].startswith("event: bars\ndata: "))
        build.assert_called_once_with(SYM, "1d", 1006, "none")

    def test_symbols_truncated_to_max(self):
        fetch = mock.Mock(return_value={})
        syms = ",".join(f"60000{i}.SH" for i in range(self.stream.QUOTE_SSE_MAX_SYMBOLS + 10))
        with mock.patch.object(self.stream.market, "fetch_quotes", fetch):
            self._frames_data(f"/api/stream/quotes?symbols={syms}")
        self.assertEqual(len(fetch.call_args.args[0]), self.stream.QUOTE_SSE_MAX_SYMBOLS)

    def test_fetch_failure_emits_keepalive_frame(self):
        with mock.patch.object(self.stream.market, "fetch_quotes",
                               side_effect=RuntimeError("upstream down")):
            _resp, frames = self._frames_data(f"/api/stream/quotes?symbols={SYM}")
        self.assertEqual(frames[0], ": tick\n\n", "单次快照失败应发保活帧而非断开")

    def test_keepalive_frame_between_snapshots(self):
        # 推送间隔未到时发 : keepalive 注释帧, 使断线能在 ~1s 内被发现并归还槽位
        self.stream.QUOTE_SSE_INTERVAL = 3600.0
        with mock.patch.object(self.stream.market, "fetch_quotes", return_value={}):
            _resp, frames = self._frames_data(f"/api/stream/quotes?symbols={SYM}", n=2)
        self.assertTrue(frames[0].startswith("data: "), "首帧应推送快照")
        self.assertEqual(frames[1], ": keepalive\n\n", "间隔未到应发保活注释帧")

    def test_429_when_slots_exhausted(self):
        for _ in range(self.stream.QUOTE_SSE_MAX_CLIENTS):
            self.assertTrue(self.stream._sse_slots.acquire(blocking=False))
        try:
            r = self.client.get(f"/api/stream/quotes?symbols={SYM}")
            self.assertEqual(r.status_code, 429)
            r.close()
        finally:
            for _ in range(self.stream.QUOTE_SSE_MAX_CLIENTS):
                self.stream._sse_slots.release()

    def test_slot_released_after_close(self):
        # 正常取满并发额度 → 关闭后槽位必须全部归还 (否则常驻线程泄漏)
        with mock.patch.object(self.stream.market, "fetch_quotes", return_value={}):
            for _ in range(self.stream.QUOTE_SSE_MAX_CLIENTS):
                self._frames(f"/api/stream/quotes?symbols={SYM}", n=1)
        got = 0
        try:
            while self.stream._sse_slots.acquire(blocking=False):
                got += 1
        finally:
            for _ in range(got):
                self.stream._sse_slots.release()
        self.assertEqual(got, self.stream.QUOTE_SSE_MAX_CLIENTS, "断开后槽位应全部归还")

    def _drain(self, url):
        """读到生成器自然结束, 返回全部帧。"""
        resp = self.client.get(url, buffered=False)
        self.assertEqual(resp.status_code, 200)
        frames = []
        try:
            for chunk in resp.iter_encoded():
                frames.append(chunk.decode("utf-8"))
        finally:
            resp.close()
        return frames

    def _count_free_slots(self):
        got = 0
        try:
            while self.stream._sse_slots.acquire(blocking=False):
                got += 1
        finally:
            for _ in range(got):
                self.stream._sse_slots.release()
        return got

    def test_max_lifetime_ends_stream_normally_and_releases_slot(self):
        self.stream.QUOTE_SSE_MAX_LIFETIME = 0.25
        self.stream.QUOTE_SSE_TICK = 0.05
        with mock.patch.object(self.stream.market, "fetch_quotes", return_value={}):
            frames = self._drain(f"/api/stream/quotes?symbols={SYM}")
        self.assertTrue(frames)
        self.assertEqual(frames[0], "retry: 5000\n\n")
        self.assertEqual(frames[-1], ": rotate\n\n", "到期应以注释帧正常结束(供浏览器重连)")
        self.assertEqual(self._count_free_slots(), self.stream.QUOTE_SSE_MAX_CLIENTS,
                         "到期结束后必须归还全部槽位")

    def test_max_lifetime_does_not_truncate_normal_push(self):
        self.stream.QUOTE_SSE_MAX_LIFETIME = 3600.0
        quotes = {SYM: {"last_price": 10.0}}
        with mock.patch.object(self.stream.market, "fetch_quotes", return_value=quotes):
            _resp, frames = self._frames_data(f"/api/stream/quotes?symbols={SYM}")
        self.assertTrue(frames[0].startswith("data: "))

    def test_slots_recover_after_all_expire(self):
        self.stream.QUOTE_SSE_MAX_LIFETIME = 0.2
        self.stream.QUOTE_SSE_TICK = 0.05
        with mock.patch.object(self.stream.market, "fetch_quotes", return_value={}):
            for _ in range(self.stream.QUOTE_SSE_MAX_CLIENTS):
                self._drain(f"/api/stream/quotes?symbols={SYM}")
        self.assertEqual(self._count_free_slots(), self.stream.QUOTE_SSE_MAX_CLIENTS,
                         "到期后新连接应可立即建立")


if __name__ == "__main__":
    unittest.main(verbosity=2)
