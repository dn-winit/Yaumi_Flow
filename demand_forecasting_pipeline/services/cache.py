"""
Lightweight TTL cache for artifact data.
Avoids re-reading CSV/JSON files on every API call.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional


class TTLCache:
    """Thread-safe key-value cache with per-entry TTL expiry."""

    def __init__(self, default_ttl: int = 300) -> None:
        self._store: dict[str, tuple[Any, float]] = {}
        self._ttl = default_ttl
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.time() > expires_at:
                del self._store[key]
                return None
            return value

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        with self._lock:
            self._store[key] = (value, time.time() + (ttl or self._ttl))

    def get_or_load(self, key: str, loader: Callable[[], Any], ttl: Optional[int] = None) -> Any:
        """Return cached value or call loader, cache result, and return."""
        val = self.get(key)
        if val is not None:
            return val
        val = loader()
        if val is not None:
            self.set(key, val, ttl)
        return val

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    @property
    def keys(self) -> list[str]:
        with self._lock:
            now = time.time()
            return [k for k, (_, exp) in self._store.items() if now <= exp]
