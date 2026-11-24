"""日K额度在 Web 进程与因子构建子进程之间共享。"""
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

VISUAL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(VISUAL))
from shared_budget import FOREGROUND_LOCK_WAIT_S, SharedTokenBucket


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

    def test_foreground_and_workers_share_allowance_across_processes(self):
        child_code = (
            "import sys; sys.path.insert(0, sys.argv[2]); "
            "from shared_budget import SharedTokenBucket; "
            "bucket = SharedTokenBucket(sys.argv[1], 4); "
            "print(sum(bucket.try_acquire() for _ in range(4)))"
        )
        with tempfile.TemporaryDirectory() as root, ThreadPoolExecutor(max_workers=3) as pool:
            path = Path(root) / "budget.sqlite3"
            bucket = SharedTokenBucket(path, rate_per_min=4)
            children = [pool.submit(subprocess.run,
                                    [sys.executable, "-B", "-c", child_code, str(path), str(VISUAL)],
                                    capture_output=True, text=True, check=True, timeout=10)
                        for _ in range(3)]
            foreground = sum(bucket.try_acquire(lock_wait_s=FOREGROUND_LOCK_WAIT_S)
                             for _ in range(4))
            self.assertEqual(foreground + sum(int(c.result().stdout) for c in children), 4)

    def test_claim_waits_out_a_short_write_lock(self):
        # 旧实现 busy 只等 0.25s，锁住 0.4s 会在 BEGIN IMMEDIATE 抛 database is locked。
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "budget.sqlite3"
            bucket = SharedTokenBucket(path, rate_per_min=4)
            self.assertTrue(bucket.try_acquire())
            started = threading.Event()

            def hold():
                conn = sqlite3.connect(str(path), timeout=2, isolation_level=None)
                conn.execute("BEGIN IMMEDIATE")
                started.set()
                time.sleep(0.4)
                conn.execute("COMMIT")
                conn.close()

            thread = threading.Thread(target=hold)
            thread.start()
            try:
                self.assertTrue(started.wait(2))
                self.assertTrue(bucket.try_acquire())
            finally:
                thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_immediate_lock_error_is_retried(self):
        # SQLite 判断可能死锁时不走 busy_timeout，直接抛 locked。去掉重试会在第一次就失败。
        with tempfile.TemporaryDirectory() as root:
            bucket = SharedTokenBucket(Path(root) / "budget.sqlite3", rate_per_min=4)
            calls = {"n": 0}
            real = bucket._claim

            def claim(n, deadline):
                calls["n"] += 1
                if calls["n"] < 3:
                    raise sqlite3.OperationalError("database is locked")
                return real(n, deadline)

            bucket._claim = claim
            started = time.monotonic()
            self.assertTrue(bucket.try_acquire())
            self.assertGreaterEqual(calls["n"], 3)
            self.assertLess(time.monotonic() - started, 1.0)

    def test_other_sqlite_errors_still_propagate(self):
        with tempfile.TemporaryDirectory() as root:
            bucket = SharedTokenBucket(Path(root) / "budget.sqlite3", rate_per_min=4)

            def claim(n, deadline):
                raise sqlite3.OperationalError("disk I/O error")

            bucket._claim = claim
            with self.assertRaises(sqlite3.OperationalError):
                bucket.try_acquire()

    def test_lock_past_budget_is_a_miss_and_does_not_take_a_token(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "budget.sqlite3"
            bucket = SharedTokenBucket(path, rate_per_min=1)
            started = threading.Event()
            release = threading.Event()

            def hold():
                conn = sqlite3.connect(str(path), timeout=3, isolation_level=None)
                conn.execute("BEGIN IMMEDIATE")
                started.set()
                release.wait(3)
                conn.execute("COMMIT")
                conn.close()

            thread = threading.Thread(target=hold)
            thread.start()
            try:
                self.assertTrue(started.wait(2))
                t0 = time.monotonic()
                self.assertFalse(bucket.try_acquire())
                elapsed = time.monotonic() - t0
            finally:
                release.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertGreater(elapsed, 0.7)
            self.assertLess(elapsed, 1.8)
            self.assertTrue(bucket.try_acquire())

    def test_foreground_returns_before_a_background_writer_releases_lock(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "budget.sqlite3"
            bucket = SharedTokenBucket(path, rate_per_min=1)
            with closing(sqlite3.connect(str(path), isolation_level=None)) as conn:
                conn.execute("BEGIN IMMEDIATE")
                start = time.monotonic()
                self.assertFalse(bucket.try_acquire(lock_wait_s=FOREGROUND_LOCK_WAIT_S))
                self.assertLess(time.monotonic() - start, 0.65)
                conn.rollback()
            self.assertTrue(bucket.try_acquire(lock_wait_s=FOREGROUND_LOCK_WAIT_S))
            self.assertFalse(bucket.try_acquire(lock_wait_s=FOREGROUND_LOCK_WAIT_S))
