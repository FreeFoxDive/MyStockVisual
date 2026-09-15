"""Regression tests for SSE trading-session gating (no external network)."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

VISUAL = Path(__file__).resolve().parents[1]
TEST = Path(__file__).resolve().parent
sys.path.insert(0, str(VISUAL)); sys.path.insert(0, str(TEST))


class BackendSseSessionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import create_app
        cls.app = create_app()

    def test_status_out_of_session_is_machine_readable(self):
        import api.stream as stream
        with self.app.test_request_context('/api/stream/status'), \
             patch.object(stream, '_require_user', return_value={'id': 1}), \
             patch.object(stream.market_hours, 'in_session', return_value=False):
            response = stream.stream_status()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            'ok': True, 'sse_allowed': False, 'in_session': False, 'retry_after': 30,
        })

    def test_stream_rejects_before_allocating_slot(self):
        import api.stream as stream
        with self.app.test_request_context('/api/stream/quotes?symbols=600519.SH'), \
             patch.object(stream, '_require_user', return_value={'id': 1}), \
             patch.object(stream.market_hours, 'in_session', return_value=False), \
             patch.object(stream, '_sse_slots') as slots:
            response = stream.stream_quotes()
        self.assertEqual(response.status_code, 425)
        body = json.loads(response.get_data(as_text=True))
        self.assertEqual(body['code'], 'SSE_OUT_OF_SESSION')
        slots.acquire.assert_not_called()

    def test_server_source_has_out_of_session_fetch_guard(self):
        source = (VISUAL / 'api' / 'stream.py').read_text(encoding='utf-8')
        guard = source.index('if not market_hours.in_session():', source.index('def generate'))
        fetch = source.index('quotes = market.fetch_quotes(symbols)', guard)
        self.assertLess(guard, fetch)


class FrontendSseSessionTest(unittest.TestCase):
    def test_production_uses_status_probe_and_polling_lock(self):
        src = (VISUAL / 'static' / 'index.html').read_text(encoding='utf-8')
        js = (VISUAL / 'static' / 'js' / 'live-market.js').read_text(encoding='utf-8')
        self.assertIn('streamStatus: true', src)
        self.assertIn("fetch('/api/stream/status'", js)
        self.assertIn('this.sseConnecting', js)
        self.assertIn('!this.sseConnecting', js)

    def test_status_probe_is_before_eventsource(self):
        js = (VISUAL / 'static' / 'js' / 'live-market.js').read_text(encoding='utf-8')
        self.assertLess(js.index("fetch('/api/stream/status'"), js.index('new global.EventSource(url)'))


if __name__ == '__main__':
    unittest.main()
