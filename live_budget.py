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
            return self._try_acquire_locked(time.monotonic())

    def _try_acquire_locked(self, now):
        """Caller holds self.lock; use one timestamp for admission and accounting."""
        while self.starts and now - self.starts[0] >= 60:
            self.starts.popleft()
        if now < self.next_at or len(self.starts) >= self.limit:
            return False
        self.starts.append(now)
        self.next_at = now + self.interval
        return True

    def acquire(self, timeout=0.0):
        """Wait for this same paced budget; never bypass its interval or rolling limit."""
        deadline = time.monotonic() + max(float(timeout), 0.0)
        # timeout=0 still permits one immediate, nonblocking admission attempt.
        if self.try_acquire():
            return True
        while True:
            with self.lock:
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    return False
                # Check the deadline and spend the token under the same lock:
                # a delayed wakeup must not admit an already timed-out request.
                if self._try_acquire_locked(now):
                    return True
                ready_at = self.next_at
                if len(self.starts) >= self.limit:
                    ready_at = max(ready_at, self.starts[0] + 60.0)
                delay = max(ready_at - now, 0.001)
            time.sleep(min(delay, remaining))
