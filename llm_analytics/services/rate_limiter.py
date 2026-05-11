"""
Token bucket rate limiter -- thread-safe.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)


class RateLimiter:
    """Token bucket rate limiter with configurable window.

    All timing knobs come from the caller (Analyzer wires them from
    Settings) so a single ``.env`` change governs both the live service
    and the in-process backfill paths -- no module-level magic numbers.
    """

    def __init__(
        self,
        max_requests: int,
        window_seconds: int,
        *,
        acquire_timeout_seconds: float,
        poll_interval_seconds: float,
    ) -> None:
        if max_requests < 1 or window_seconds <= 0:
            raise ValueError("max_requests and window_seconds must be positive")
        if poll_interval_seconds <= 0 or acquire_timeout_seconds <= 0:
            raise ValueError("acquire/poll intervals must be positive")
        self._max = int(max_requests)
        self._window = float(window_seconds)
        self._tokens = float(max_requests)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()
        self._acquire_timeout = float(acquire_timeout_seconds)
        self._poll_interval = float(poll_interval_seconds)

    def acquire(self, timeout: float | None = None) -> bool:
        """Try to acquire a token. ``True`` if allowed, ``False`` if
        the deadline elapses before a token frees up.
        """
        wait = self._acquire_timeout if timeout is None else float(timeout)
        deadline = time.monotonic() + wait

        while True:
            with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True

            if time.monotonic() >= deadline:
                return False
            time.sleep(self._poll_interval)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        if elapsed <= 0:
            return
        self._tokens = min(
            float(self._max),
            self._tokens + elapsed * (self._max / self._window),
        )
        self._last_refill = now
