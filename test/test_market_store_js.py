import subprocess
import sys
import unittest
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from js_test_util import require_node


class MarketStoreJsTest(unittest.TestCase):
    def test_state_ordering_and_resource_isolation(self):
        require_node()
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ['node', str(root / 'test/market_store_cases.js'), str(root)],
            capture_output=True, text=True, encoding='utf-8', timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main(verbosity=2)
