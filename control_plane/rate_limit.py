from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowRateLimiter:
    """In-process per-key sliding-window limiter.

    Tracks request timestamps per client key (IP) in a bounded window and rejects
    once the count in the trailing window reaches the limit. Thread-safe because
    FastAPI serves sync path operations from a threadpool.

    Scope note: state is per-process, so behind multiple workers the effective
    limit is (limit x workers). For a hard global limit, put a shared limiter
    (e.g. a reverse proxy or Redis-backed limiter) in front. This layer is a
    always-on backstop against brute-force and accidental floods.
    """

    def __init__(self, limit: int, window_seconds: float = 60.0, max_keys: int = 100_000):
        self.limit = limit
        self.window = window_seconds
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def check(self, key: str, now: float | None = None) -> tuple[bool, float]:
        """Return (allowed, retry_after_seconds). retry_after is 0 when allowed."""
        if not self.enabled:
            return True, 0.0

        now = time.monotonic() if now is None else now
        cutoff = now - self.window
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                # Cheap unbounded-growth guard: drop the whole table if it explodes.
                if len(self._hits) >= self.max_keys:
                    self._hits.clear()
                bucket = self._hits[key] = deque()

            while bucket and bucket[0] <= cutoff:
                bucket.popleft()

            if len(bucket) >= self.limit:
                retry_after = self.window - (now - bucket[0])
                return False, max(retry_after, 1.0)

            bucket.append(now)
            return True, 0.0
