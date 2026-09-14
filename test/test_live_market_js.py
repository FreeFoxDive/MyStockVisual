import subprocess
import unittest
from pathlib import Path

from js_test_util import require_node


class LiveMarketTest(unittest.TestCase):
    def test_transport_boundaries(self):
        require_node()
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ['node', str(root / 'test/live_market_cases.js'), str(root / 'static/js/live-market.js')],
            capture_output=True, text=True, encoding='utf-8', timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
