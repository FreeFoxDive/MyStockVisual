"""进程间共享的 AlphaFeed 令牌桶。状态文件须位于各进程共用的缓存目录。"""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path

FOREGROUND_LOCK_WAIT_S = 0.25


class SharedTokenBucket:
    """用 SQLite 写事务串行化取令牌；取不到令牌时不等待。"""

    def __init__(self, path, rate_per_min):
        self.path = Path(path)
        self.capacity = float(rate_per_min)
        self.rate = self.capacity / 60.0

    def try_acquire(self, n=1, *, lock_wait_s=1.0):
        """只为 SQLite 锁等待；额度不足时立即返回 False。"""
        if n <= 0:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 重试只消化「立刻返回的 locked」。每轮只用剩余时间，到点返回 False，不把超时乘倍。
        deadline = time.monotonic() + lock_wait_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                return self._claim(n, deadline)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                pause = deadline - time.monotonic()
                if pause <= 0:
                    return False
                time.sleep(min(0.02, pause))

    def _claim(self, n, deadline):
        # 独立于 factors.db，构建写 bars 时不能挡住图表取令牌。
        # 保留数据库既有日志模式；取令牌时不争抢切换日志模式所需的锁。
        with closing(sqlite3.connect(str(self.path), timeout=0, isolation_level=None)) as conn:
            def set_remaining_timeout():
                ms = max(0, int((deadline - time.monotonic()) * 1000))
                conn.execute(f"PRAGMA busy_timeout={ms}")

            set_remaining_timeout()
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("CREATE TABLE IF NOT EXISTS bucket ("
                             "name TEXT PRIMARY KEY, tokens REAL NOT NULL, updated REAL NOT NULL)")
                row = conn.execute(
                    "SELECT tokens, updated FROM bucket WHERE name='daily'").fetchone()
                now = time.time()
                tokens = self.capacity if row is None else min(
                    self.capacity, row[0] + max(0.0, now - row[1]) * self.rate)
                allowed = tokens >= n
                if not allowed:
                    return False
                conn.execute(
                    "INSERT INTO bucket(name, tokens, updated) VALUES('daily', ?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET tokens=excluded.tokens, "
                    "updated=excluded.updated",
                    (tokens - n, now))
                # 回滚日志模式下，COMMIT 也可能等待读者释放锁，不能再用一整轮预算。
                set_remaining_timeout()
                conn.commit()
                return True
            finally:
                if conn.in_transaction:
                    conn.rollback()
