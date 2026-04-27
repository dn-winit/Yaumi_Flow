"""
Token bucket rate limiter -- thread-safe.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

# How long acquire() waits for a token before giving up. Sized to be
# noticeably shorter than the LLM client timeout so a request that
# can't get a token fails fast instead of stacking on the budget.
_DEFAULT_ACQUIRE_TIMEOUT_SECONDS = 5.0
# Poll interval between token re-checks while waiting. Small enough to
# feel responsive, large enough that the busy loop is not a CPU sink.
_ACQUIRE_POLL_INTERVAL_SECONDS = 0.1


class RateLimiter:
    """Token bucket rate limiter with configurable window."""

    def __init__(self, max_requests: int = 10, window_seconds: int = 60) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._tokens = float(max_requests)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = _DEFAULT_ACQUIRE_TIMEOUT_SECONDS) -> bool:
        """Try to acquire a token. Returns True if allowed, False if rate limited."""
        deadline = time.monotonic() + timeout

        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True

            if time.monotonic() >= deadline:
                return False
            time.sleep(_ACQUIRE_POLL_INTERVAL_SECONDS)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        refill = elapsed * (self._max / self._window)
        self._tokens = min(float(self._max), self._tokens + refill)
        self._last_refill = now
