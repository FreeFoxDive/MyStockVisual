"""Regression tests for SSE trading-session gating (no external network).

建流门控 (can_connect_stream) 与取数门控 (is_live) 是两条不同的线:
  - 交易日 09:00 起就允许建流 (盘前只发保活帧, 不取上游数据), 开盘首帧零握手延迟
  - 只有 is_live (09:15-11:30 / 13:00-15:00) 才真的向上游要数据
所以"能不能连"不再等于"是不是盘中", 这两条都要钉住。
"""
import json
from datetime import datetime
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

VISUAL = Path(__file__).resolve().parents[1]
TEST = Path(__file__).resolve().parent
sys.path.insert(0, str(VISUAL)); sys.path.insert(0, str(TEST))


def _at(when):
    return patch('market_hours.now', return_value=datetime.strptime(when, '%Y-%m-%d %H:%M'))


class BackendSseSessionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import create_app
        cls.app = create_app()

    def _status(self, when, user=True):
        import api.stream as stream
        with _at(when), self.app.test_request_context('/api/stream/status'):
            with patch.object(stream, '_require_user',
                              return_value={'id': 1} if user else None):
                return stream.stream_status()

    def test_status_reports_phase_and_next_wake(self):
        response = self._status('2026-09-17 09:23')   # 集合竞价
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['session_phase'], 'auction')
        self.assertTrue(body['quote_live'], '竞价期已有数据')
        self.assertFalse(body['in_session'], 'in_session 语义不变: 仍只表示连续竞价')
        self.assertTrue(body['sse_allowed'])
        self.assertEqual(body['retry_after'], 5, '可连时快速重连')
        self.assertIsNone(body['next_live_in_sec'], '已在活跃时段')

    def test_status_out_of_session_is_machine_readable(self):
        """盘外: 明确不允许 + 给出下一次可连的秒数 (客户端据此自己回来)。"""
        response = self._status('2026-09-19 10:00')   # 周六
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertFalse(body['sse_allowed'])
        self.assertEqual(body['session_phase'], 'non_trading')
        self.assertFalse(body['quote_live'])
        self.assertEqual(body['retry_after'], 1800, '钳上界: 长假最多半小时复探一次')
        self.assertEqual(body['next_live_at'], '2026-09-21 09:15:00')

    def test_status_pre_open_keeps_the_hint_short(self):
        """开盘前 10 分钟: 提示就应该是那 10 分钟, 不是固定 30s 也不是半小时。"""
        body = self._status('2026-09-17 08:50').get_json()
        self.assertFalse(body['sse_allowed'])
        self.assertEqual(body['retry_after'], 600)
        self.assertEqual(body['next_live_in_sec'], 1500.0)

    def test_status_requires_login(self):
        self.assertEqual(self._status('2026-09-17 09:23', user=False).status_code, 401)

    def test_stream_rejects_before_allocating_slot(self):
        import api.stream as stream
        with _at('2026-09-19 10:00'), \
             self.app.test_request_context('/api/stream/quotes?symbols=600519.SH'), \
             patch.object(stream, '_require_user', return_value={'id': 1}), \
             patch.object(stream, '_sse_slots') as slots:
            response = stream.stream_quotes()
        self.assertEqual(response.status_code, 425)
        body = json.loads(response.get_data(as_text=True))
        self.assertEqual(body['code'], 'SSE_OUT_OF_SESSION')
        self.assertEqual(body['retry_after'], 1800)
        self.assertEqual(response.headers['Retry-After'], '1800')
        slots.acquire.assert_not_called()

    def test_stream_allowed_before_data_starts(self):
        """09:00-09:15 允许建流: 只发保活帧不取数, 换来开盘首帧零握手延迟。"""
        import api.stream as stream
        with _at('2026-09-17 09:10'), \
             self.app.test_request_context('/api/stream/quotes?symbols=600519.SH'), \
             patch.object(stream, '_require_user', return_value={'id': 1}), \
             patch.object(stream.market, 'fetch_quotes') as fetch:
            response = stream.stream_quotes()
            frames = []
            for chunk in response.response:
                frames.append(chunk if isinstance(chunk, str) else chunk.decode('utf-8'))
                if len(frames) >= 2:
                    break
            response.close()
        self.assertNotEqual(response.status_code, 425, '盘前应允许建立')
        self.assertEqual(frames[0], 'retry: 5000\n\n')
        self.assertTrue(frames[1].startswith('event: market\ndata: '), '首帧即报相位')
        self.assertEqual(json.loads(frames[1][len('event: market\ndata: '):])['session_phase'],
                         'pre')
        fetch.assert_not_called()

    def test_server_source_gates_fetch_on_live_not_session(self):
        source = (VISUAL / 'api' / 'stream.py').read_text(encoding='utf-8')
        generate = source.index('def generate')
        guard = source.index('if not market_hours.is_live():', generate)
        fetch = source.index('quotes = market.fetch_quotes(symbols)', guard)
        self.assertLess(guard, fetch, '取数必须在活跃时段门控之后')
        # 建流门控是另一条线: 不能退回 in_session (那样 09:00-09:15 连不上)
        self.assertIn('if not market_hours.can_connect_stream():', source)


class FrontendSseSessionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = (VISUAL / 'static' / 'index.html').read_text(encoding='utf-8')
        cls.js = (VISUAL / 'static' / 'js' / 'live-market.js').read_text(encoding='utf-8')
        cls.clock = (VISUAL / 'static' / 'js' / 'market-clock.js').read_text(encoding='utf-8')

    def test_production_uses_status_probe_and_polling_lock(self):
        self.assertIn('streamStatus: true', self.src)
        self.assertIn("fetch('/api/stream/status'", self.js)
        # connect() 用 sseConnecting 阻止重复建立连接 (轮询锁)。
        self.assertIn('this.sseConnecting', self.js)

    def test_status_probe_is_before_eventsource(self):
        self.assertLess(self.js.index("fetch('/api/stream/status'"),
                        self.js.index('new global.EventSource(url)'))

    def test_pages_wire_the_market_clock(self):
        """三页共用同一份时段口径: 各自的 inAshareSession/syncSessionFlag 已删除。"""
        for page in ('index.html', 'monitor.html', 'trades.html'):
            html = (VISUAL / 'static' / page).read_text(encoding='utf-8')
            with self.subTest(page=page):
                self.assertIn('/js/market-clock.js', html, '必须加载共享时钟')
                self.assertIn('new VisualMarketClock.MarketClock(', html)
                self.assertIn('marketClock.start()', html)
                self.assertIn('onMarket:', html, '相位帧要交给时钟')
                self.assertIn('streamActive:', html, '建流门槛与取数门槛分离')
                self.assertNotIn('_serverInSession', html, '旧的单次 ping 缓存必须删掉')

    def test_stream_state_feedback_loop(self):
        """时钟要知道流活没活: 活着停探测, 断了恢复探测。"""
        self.assertIn('onStreamState', self.src)
        self.assertIn('marketClock.setStreamConnected', self.src)
        self.assertIn('_noteAlive(true)', self.js, '有效帧才算活着 (OPEN 不算)')
        self.assertIn('_noteAlive(false)', self.js)

    def test_clock_documents_the_relative_wake_contract(self):
        """唤醒必须用服务端给的相对秒数, 不能拿本地时钟比绝对时刻。"""
        self.assertIn('next_live_in_sec', self.clock)
        self.assertNotIn('next_live_at - ', self.clock)
        self.assertIn('wakeMs', self.clock)


if __name__ == '__main__':
    unittest.main()
