"""No-burst, rolling-window budgets shared by all HTTP and SSE callers in a worker."""
import threading
import time
from collections import deque


class PacedBudget:
    def __init__(self, per_minute):
        self.limit = max(1, int(per_minute))
        self.interval = 60.0 / self.limit
        self.starts = deque()
        self.next_at = 0.0
        self.lock = threading.Lock()

    def try_acquire(self, n=1):
        if n != 1:
            return False
        with self.lock:
            now = time.monotonic()
            while self.starts and now - self.starts[0] >= 60:
                self.starts.popleft()
            if now < self.next_at or len(self.starts) >= self.limit:
                return False
            self.starts.append(now)
            self.next_at = now + self.interval
            return True
