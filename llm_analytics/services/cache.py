"""
File-based LLM response cache with TTL.

Layout
------
Cache files live under ``cache/{xx}/{key}.json`` where ``xx`` is the
last two hex chars of the digest (256 shards). A flat layout hits
filesystem perf cliffs around 10-100k entries on Windows + ext4; the
shard split keeps every directory listing small and bounds the work of
the lazy janitor.

Migration is one-shot at construction: any flat ``cache/*.json`` file
left over from the pre-shard layout is moved into its shard before the
cache starts serving traffic. Idempotent -- a flat-free run is a no-op.

Concurrency
-----------
File I/O happens **outside** the cache lock; the lock only protects the
hits/misses/entry-count counters. Concurrent readers don't serialise on
disk reads, and counter increments stay race-free under load.

Atomicity
---------
Writes go to a sibling ``.json.tmp`` and are renamed via ``os.replace``
(atomic on Windows + POSIX). A crash mid-write cannot leave a partially
written entry that a later read would mistake for valid cached data.

Janitor
-------
Every ``cache_janitor_every_n_writes`` writes, one shard's expired (and
corrupt) entries are purged. Round-robin across shards so the worst-case
cost stays bounded (~entries-per-shard) instead of an O(N) full-tree walk.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class LLMCache:
    """Thread-safe sharded file cache with per-entry TTL."""

    _SUFFIX = ".json"
    _TMP_SUFFIX = ".json.tmp"
    _DIGEST_LEN = 16            # hex chars from sha256 -- same as before
    _SHARD_LEN = 2              # last 2 hex chars of digest -> 256 shards

    def __init__(
        self,
        cache_dir: str,
        ttl_hours: int = 24,
        enabled: bool = True,
        *,
        janitor_every_n_writes: int = 200,
    ) -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._ttl = timedelta(hours=ttl_hours)
        self._enabled = enabled

        # Counters under a small dedicated lock so they stay coherent
        # without serialising file I/O.
        self._stats_lock = threading.Lock()
        self.hits = 0
        self.misses = 0

        # Janitor state.
        self._janitor_every = max(1, int(janitor_every_n_writes))
        self._writes_since_janitor = 0
        self._janitor_idx = -1

        if self._enabled:
            self._migrate_flat_to_shards()
            self._entry_count = self._calibrate_count()
        else:
            self._entry_count = 0

    # ------------------------------------------------------------------
    # Key + path helpers
    # ------------------------------------------------------------------

    @classmethod
    def _make_key(cls, prefix: str, **kwargs: Any) -> str:
        sorted_params = json.dumps(sorted(kwargs.items()), sort_keys=True, default=str)
        digest = hashlib.sha256(sorted_params.encode()).hexdigest()[: cls._DIGEST_LEN]
        return f"{prefix}_{digest}"

    def _shard_for(self, key: str) -> str:
        # Use the tail of the digest (which is the tail of ``key``) so
        # the entropy across shards is uniform regardless of prefix.
        return key[-self._SHARD_LEN:]

    def _path_for(self, key: str) -> Path:
        return self._dir / self._shard_for(key) / f"{key}{self._SUFFIX}"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, prefix: str, **kwargs) -> Optional[Dict[str, Any]]:
        if not self._enabled:
            return None
        key = self._make_key(prefix, **kwargs)
        path = self._path_for(key)

        if not path.exists():
            self._inc_miss()
            return None

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            expires = datetime.fromisoformat(data["expires_at"])
            if datetime.now() >= expires:
                self._unlink_and_decr(path)
                self._inc_miss()
                return None
            self._inc_hit()
            return data["response"]
        except Exception as exc:
            logger.warning("Cache read error for %s: %s", key, exc)
            # Corrupt or unreadable -- treat as miss + try to clean up
            # so a subsequent set() doesn't have to step over it.
            self._unlink_and_decr(path)
            self._inc_miss()
            return None

    def set(self, prefix: str, response: Dict[str, Any], **kwargs) -> None:
        if not self._enabled:
            return
        key = self._make_key(prefix, **kwargs)
        path = self._path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        entry = {
            "response": response,
            "expires_at": (datetime.now() + self._ttl).isoformat(),
            "created_at": datetime.now().isoformat(),
            "key": key,
        }

        existed = path.exists()
        tmp = path.with_suffix(self._TMP_SUFFIX)
        try:
            tmp.write_text(json.dumps(entry, default=str), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as exc:
            logger.warning("Cache write error for %s: %s", key, exc)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return

        if not existed:
            with self._stats_lock:
                self._entry_count += 1

        self._maybe_run_janitor()

    def clear(self) -> int:
        count = 0
        for f in self._dir.rglob(f"*{self._SUFFIX}"):
            try:
                f.unlink(missing_ok=True)
                count += 1
            except Exception:
                pass
        with self._stats_lock:
            self.hits = 0
            self.misses = 0
            self._entry_count = 0
        return count

    def stats(self) -> Dict[str, Any]:
        with self._stats_lock:
            hits, misses, entries = self.hits, self.misses, self._entry_count
        total = hits + misses
        return {
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / max(total, 1) * 100, 1),
            "cached_entries": entries,
        }

    # ------------------------------------------------------------------
    # Internal: counters
    # ------------------------------------------------------------------

    def _inc_hit(self) -> None:
        with self._stats_lock:
            self.hits += 1

    def _inc_miss(self) -> None:
        with self._stats_lock:
            self.misses += 1

    def _unlink_and_decr(self, path: Path) -> None:
        try:
            existed = path.exists()
            path.unlink(missing_ok=True)
        except Exception:
            return
        if existed:
            with self._stats_lock:
                self._entry_count = max(0, self._entry_count - 1)

    # ------------------------------------------------------------------
    # Internal: migration + calibration
    # ------------------------------------------------------------------

    def _migrate_flat_to_shards(self) -> int:
        """Move any pre-shard ``cache/*.json`` files into their shard.

        Walks only the top-level directory (not into shards) so a steady-
        state run with only sharded files completes in a single empty glob.
        """
        moved = 0
        for f in self._dir.glob(f"*{self._SUFFIX}"):
            if not f.is_file():
                continue
            target = self._path_for(f.stem)
            if target.exists():
                # Sharded copy already in place; the flat one is stale.
                f.unlink(missing_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                f.replace(target)
                moved += 1
            except Exception as exc:
                logger.warning("Migrate %s -> %s failed: %s", f, target, exc)
        if moved:
            logger.info("Cache: migrated %d flat entries to sharded layout", moved)
        return moved

    def _calibrate_count(self) -> int:
        """One-time walk to seed the in-memory counter so ``stats()`` is
        O(1) thereafter. Pays for itself the first time ``health()`` runs.
        """
        return sum(1 for _ in self._dir.rglob(f"*{self._SUFFIX}"))

    # ------------------------------------------------------------------
    # Internal: janitor
    # ------------------------------------------------------------------

    def _maybe_run_janitor(self) -> None:
        with self._stats_lock:
            self._writes_since_janitor += 1
            due = self._writes_since_janitor >= self._janitor_every
            if due:
                self._writes_since_janitor = 0
        if due:
            try:
                self._purge_one_shard()
            except Exception as exc:
                # Janitor must never break the write path.
                logger.warning("Cache janitor error: %s", exc)

    def _purge_one_shard(self) -> None:
        shards = sorted(d for d in self._dir.iterdir() if d.is_dir())
        if not shards:
            return
        with self._stats_lock:
            self._janitor_idx = (self._janitor_idx + 1) % len(shards)
            idx = self._janitor_idx
        shard = shards[idx]

        purged = 0
        now = datetime.now()
        for f in shard.glob(f"*{self._SUFFIX}"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                expires = datetime.fromisoformat(data["expires_at"])
                if now >= expires:
                    f.unlink(missing_ok=True)
                    purged += 1
            except Exception:
                # Corrupt entries get the same fate as expired ones --
                # they would only fail subsequent reads anyway.
                f.unlink(missing_ok=True)
                purged += 1

        if purged:
            with self._stats_lock:
                self._entry_count = max(0, self._entry_count - purged)
            logger.info("Cache janitor: shard=%s purged=%d", shard.name, purged)
