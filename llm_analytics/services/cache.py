"""
File-based LLM response cache with TTL.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class LLMCache:
    """Thread-safe file-based cache with per-entry expiration."""

    def __init__(self, cache_dir: str, ttl_hours: int = 24, enabled: bool = True) -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._ttl = timedelta(hours=ttl_hours)
        self._enabled = enabled
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _make_key(prefix: str, **kwargs) -> str:
        sorted_params = json.dumps(sorted(kwargs.items()), sort_keys=True)
        digest = hashlib.sha256(sorted_params.encode()).hexdigest()[:16]
        return f"{prefix}_{digest}"

    def get(self, prefix: str, **kwargs) -> Optional[Dict[str, Any]]:
        if not self._enabled:
            return None
        key = self._make_key(prefix, **kwargs)
        path = self._dir / f"{key}.json"

        if not path.exists():
            self.misses += 1
            return None

        try:
            with self._lock:
                data = json.loads(path.read_text(encoding="utf-8"))

            expires = datetime.fromisoformat(data["expires_at"])
            if datetime.now() >= expires:
                path.unlink(missing_ok=True)
                self.misses += 1
                return None

            self.hits += 1
            return data["response"]
        except Exception as exc:
            logger.warning("Cache read error for %s: %s", key, exc)
            self.misses += 1
            return None

    def set(self, prefix: str, response: Dict[str, Any], **kwargs) -> None:
        if not self._enabled:
            return
        key = self._make_key(prefix, **kwargs)
        path = self._dir / f"{key}.json"

        entry = {
            "response": response,
            "expires_at": (datetime.now() + self._ttl).isoformat(),
            "created_at": datetime.now().isoformat(),
            "key": key,
        }

        try:
            with self._lock:
                path.write_text(json.dumps(entry, default=str), encoding="utf-8")
        except Exception as exc:
            logger.warning("Cache write error for %s: %s", key, exc)

    def clear(self) -> int:
        count = 0
        for f in self._dir.glob("*.json"):
            f.unlink(missing_ok=True)
            count += 1
        self.hits = 0
        self.misses = 0
        return count

    def stats(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": round(self.hits / max(total, 1) * 100, 1),
            "cached_entries": len(list(self._dir.glob("*.json"))),
        }
