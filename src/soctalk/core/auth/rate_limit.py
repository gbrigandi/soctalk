"""In-process rate limiter for login attempts.

internal-auth §7 calls for per-IP and per-email throttling before DB lookup, with
the note "in-process for beta; swap for Redis when horizontal scale is
needed." This module is the in-process implementation.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from time import monotonic



# Policy: 10 attempts per window per key, 15-minute window.
MAX_ATTEMPTS = 10
WINDOW_SECONDS = 15 * 60


@dataclass
class _Bucket:
    hits: deque[float]


class RateLimiter:
    """Single-process sliding-window counter. Not shared across workers."""

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}

    def hit(self, key: str) -> bool:
        """Record an attempt and return True if it's allowed."""

        now = monotonic()
        bucket = self._buckets.setdefault(key, _Bucket(hits=deque()))
        # Drop expired timestamps.
        horizon = now - WINDOW_SECONDS
        while bucket.hits and bucket.hits[0] < horizon:
            bucket.hits.popleft()
        if len(bucket.hits) >= MAX_ATTEMPTS:
            return False
        bucket.hits.append(now)
        return True

    def reset(self, key: str) -> None:
        self._buckets.pop(key, None)


_DEFAULT: RateLimiter | None = None


def default_limiter() -> RateLimiter:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = RateLimiter()
    return _DEFAULT


def reset_default_limiter() -> None:
    """For tests."""

    global _DEFAULT
    _DEFAULT = None
