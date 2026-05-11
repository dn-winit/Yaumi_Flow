"""
Lightweight TTL cache for artifact data.
Avoids re-reading CSV/JSON files on every API call.

``get_or_load`` is single-flight: when many threads miss the cache for
the same key, only one runs the loader; the rest block on a per-key
event and observe the freshly cached value once the loader returns.
This matters for hot keys like ``future_forecast`` whose loader does a
multi-MB CSV parse - without single-flight, a burst of API requests
would each spawn its own pandas read in parallel and pin RAM.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Optional

# Re-entrant so a loader can recursively touch other entries on the same
# cache (e.g. one artifact reader pulling another) without deadlocking
# the lock it already holds for its own entry.
_GLOBAL_LOCK_FACTORY = threading.RLock

class TTLCache:
    """Thread-safe key-value cache with per-entry TTL expiry.

    Internals:
      * ``_store`` -- mapping ``key -> (value, expires_at)``.
      * ``_loaders`` -- per-key ``threading.Event`` that fires when a
        single-flight loader completes (success OR failure).
      * One process-wide ``RLock`` guards both maps. RLock so a loader
        can call back into the cache via ``get`` / ``set``.
    """

    def __init__(self, default_ttl: int = 300) -> None:
        self._store: dict[str, tuple[Any, float]] = {}
        self._loaders: dict[str, threading.Event] = {}
        self._ttl = default_ttl
        self._lock = _GLOBAL_LOCK_FACTORY()

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

    def get_or_load(
        self,
        key: str,
        loader: Callable[[], Any],
        ttl: Optional[int] = None,
    ) -> Any:
        """Single-flight cached load.

        Behaviour:
          1. Cache hit -> return value (no loader call).
          2. Cache miss, no in-flight loader -> claim the key, run
             ``loader``, store, signal waiters, return.
          3. Cache miss, loader in flight -> wait for the existing
             loader to finish, then read the freshly cached value.

        ``None`` from ``loader`` is **not cached** so a transient empty
        artifact doesn't pin a missing-data response for the full TTL.
        Re-raised exceptions also skip caching.
        """
        # Fast path: cached value still fresh.
        cached = self.get(key)
        if cached is not None:
            return cached

        # Decide whether we run the loader or wait for the in-flight one.
        with self._lock:
            cached = self._store.get(key)
            if cached is not None and time.time() <= cached[1]:
                return cached[0]
            existing_event = self._loaders.get(key)
            if existing_event is None:
                event = threading.Event()
                self._loaders[key] = event
                claimed = True
            else:
                event = existing_event
                claimed = False

        if not claimed:
            # Another thread is loading. Wait without holding the lock,
            # then read what they cached. ``wait`` is unbounded so a slow
            # loader behaves like the previous serial behaviour from the
            # caller's perspective (modulo the wakeup hop).
            event.wait()
            return self.get(key)

        # We claimed the load. Always clear the loader event before
        # returning so waiters wake even if loader raises.
        try:
            value = loader()
        except Exception:
            with self._lock:
                self._loaders.pop(key, None)
            event.set()
            raise

        with self._lock:
            if value is not None:
                self._store[key] = (value, time.time() + (ttl or self._ttl))
            self._loaders.pop(key, None)
        event.set()
        return value

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
