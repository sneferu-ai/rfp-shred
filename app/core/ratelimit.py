"""In-memory token-bucket rate limiting (FR-030).

Known limitation (documented in FR-030): buckets are process-local, reset on
restart, and are not shared across replicas or Uvicorn workers; the compose
entrypoint pins ``uvicorn --workers 1``. At beta scale this is acceptable.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    count: int
    window_start: float


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._now = time.monotonic  # injectable for tests

    def hit(self, name: str, key: str, limit: int, window_s: int) -> tuple[bool, int]:
        """Record one attempt. Returns (allowed, retry_after_seconds)."""
        now = self._now()
        k = (name, key)
        with self._lock:
            bucket = self._buckets.get(k)
            if bucket is None or now - bucket.window_start >= window_s:
                bucket = _Bucket(count=0, window_start=now)
                self._buckets[k] = bucket
            bucket.count += 1
            if bucket.count <= limit:
                return True, 0
            retry_after = int(window_s - (now - bucket.window_start)) + 1
            return False, max(retry_after, 1)

    def peek(self, name: str, key: str, limit: int, window_s: int) -> tuple[bool, int]:
        """Check WITHOUT recording — for lockout checks that must not count
        the attempt being made (FR-002: 429 while locked out, even for a
        correct password; only failures increment)."""
        now = self._now()
        k = (name, key)
        with self._lock:
            bucket = self._buckets.get(k)
            if bucket is None or now - bucket.window_start >= window_s:
                return True, 0
            if bucket.count < limit:
                return True, 0
            retry_after = int(window_s - (now - bucket.window_start)) + 1
            return False, max(retry_after, 1)

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


# FR-030 limits: (limit, window_seconds)
LIMITS: dict[str, tuple[int, int]] = {
    "upload": (10, 3600),          # uploads per hour per account
    "row_patch": (100, 60),        # PATCH /api/rows per minute per account
    "checkout": (5, 600),          # checkout per 10 minutes per account
    "password_reset": (3, 3600),   # per hour per email
    "signup": (5, 3600),           # per hour per IP
    "login": (10, 300),            # FR-002: 10 failures per 5 minutes per IP
}
