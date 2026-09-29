"""进程间共享的 AlphaFeed 令牌桶。状态文件须位于各进程共用的缓存目录。"""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from pathlib import Path


class SharedTokenBucket:
    """用 SQLite 写事务串行化取令牌；取不到令牌时不等待。"""

    def __init__(self, path, rate_per_min):
        self.path = Path(path)
        self.capacity = float(rate_per_min)
        self.rate = self.capacity / 60.0

    def try_acquire(self, n=1):
        if n <= 0:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 独立于 factors.db，构建写 bars 时不能挡住图表取令牌。
        with closing(sqlite3.connect(str(self.path), timeout=0.25,
                                     isolation_level=None)) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS bucket ("
                         "name TEXT PRIMARY KEY, tokens REAL NOT NULL, updated REAL NOT NULL)")
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT tokens, updated FROM bucket WHERE name='daily'").fetchone()
                now = time.time()
                tokens = self.capacity if row is None else min(
                    self.capacity, row[0] + max(0.0, now - row[1]) * self.rate)
                allowed = tokens >= n
                if allowed:
                    tokens -= n
                conn.execute("INSERT INTO bucket(name, tokens, updated) VALUES('daily', ?, ?) "
                             "ON CONFLICT(name) DO UPDATE SET tokens=excluded.tokens, "
                             "updated=excluded.updated", (tokens, now))
                conn.commit()
                return allowed
            except Exception:
                conn.rollback()
                raise
