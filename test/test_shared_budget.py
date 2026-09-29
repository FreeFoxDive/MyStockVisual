"""日K额度在 Web 进程与因子构建子进程之间共享。"""
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VISUAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VISUAL))
from shared_budget import SharedTokenBucket


class SharedBudgetTest(unittest.TestCase):
    def test_child_cannot_refill_parent_allowance(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "budget.sqlite3"
            parent = SharedTokenBucket(path, rate_per_min=4)
            self.assertEqual([parent.try_acquire() for _ in range(4)], [True] * 4)
            child = subprocess.run(
                [sys.executable, "-c",
                 "import sys; sys.path.insert(0, sys.argv[2]); "
                 "from shared_budget import SharedTokenBucket; "
                 "print(SharedTokenBucket(sys.argv[1], 4).try_acquire())",
                 str(path), str(VISUAL)], capture_output=True, text=True, check=True)
            self.assertEqual(child.stdout.strip(), "False")

    def test_concurrent_claims_do_not_overissue(self):
        with tempfile.TemporaryDirectory() as root:
            bucket = SharedTokenBucket(Path(root) / "budget.sqlite3", rate_per_min=4)
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: bucket.try_acquire(), range(8)))
            self.assertEqual(sum(results), 4)
