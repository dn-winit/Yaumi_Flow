"""
Per-generator observability (Sprint-3, Theme D).

Two sinks:

* ``LastGenerationTracker`` -- in-memory store of the most recent generation
  run's per-route, per-generator counts + source mix + calibration snapshot.
  Exposed via the ``/metrics/last-generation`` API.
* ``MetricsCsvSink`` -- append-only single-line summary CSV under
  ``data/generation_metrics.csv`` for trend analysis. Rotates at
  ``SafetyClamps.generation_metrics_max_bytes`` by renaming the current file
  to ``generation_metrics.<rotation-idx>.csv`` and starting a fresh header.

Both sinks are thread-safe. Neither is on the hot path -- a single lock
around append / snapshot is plenty.
"""

from __future__ import annotations

import csv
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from recommended_order.config.constants import SafetyClamps

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CSV sink (append-only, rotated)
# ---------------------------------------------------------------------------

_CSV_COLUMNS = (
    "timestamp",
    "date",
    "route",
    "gen",
    "candidates",
    "kept",
    "source_pct",
    "similarity_avg",
    "calibration_fallback",
)


class MetricsCsvSink:
    """Append-only CSV trend log for generation metrics."""

    def __init__(self, path: Path, max_bytes: int) -> None:
        self._path = Path(path)
        self._max_bytes = int(max_bytes)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return
        with self._lock:
            self._rotate_if_needed()
            exists = self._path.exists()
            with self._path.open("a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
                if not exists:
                    writer.writeheader()
                for row in rows:
                    writer.writerow({c: row.get(c, "") for c in _CSV_COLUMNS})

    def _rotate_if_needed(self) -> None:
        try:
            if not self._path.exists():
                return
            if self._path.stat().st_size < self._max_bytes:
                return
        except OSError:
            return
        # Rotate: find the next index that isn't taken.
        idx = 1
        while True:
            dest = self._path.with_name(f"{self._path.stem}.{idx}{self._path.suffix}")
            if not dest.exists():
                break
            idx += 1
        try:
            self._path.rename(dest)
            logger.info("Rotated generation metrics CSV -> %s", dest.name)
        except OSError as exc:
            logger.warning("Failed to rotate metrics CSV: %s", exc)


# ---------------------------------------------------------------------------
# Last-generation tracker (for /metrics/last-generation endpoint)
# ---------------------------------------------------------------------------

@dataclass
class _DurationRing:
    """Rolling window of the last N generation durations (seconds)."""
    capacity: int = 5
    values: List[float] = field(default_factory=list)

    def add(self, secs: float) -> None:
        self.values.append(float(secs))
        if len(self.values) > self.capacity:
            self.values = self.values[-self.capacity:]

    @property
    def avg(self) -> float:
        return sum(self.values) / len(self.values) if self.values else 0.0


class LastGenerationTracker:
    """In-memory snapshot of the most recent generation run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_run_at: Optional[str] = None
        self._last_target_date: Optional[str] = None
        # route -> { "generated_at": iso, "gens": [...], "calibration": {...} }
        self._routes: Dict[str, Dict[str, Any]] = {}
        self._durations = _DurationRing()

    def record(
        self,
        *,
        route_code: str,
        target_date: str,
        gen_metrics: List[Dict[str, Any]],
        calibration_summary: Dict[str, Any],
        duration_seconds: float,
    ) -> None:
        now = datetime.utcnow().isoformat()
        with self._lock:
            self._last_run_at = now
            self._last_target_date = target_date
            self._routes[str(route_code)] = {
                "generated_at": now,
                "target_date": target_date,
                "gens": gen_metrics,
                "calibration": calibration_summary,
                "duration_seconds": round(float(duration_seconds), 3),
            }
            self._durations.add(duration_seconds)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "last_run_at": self._last_run_at,
                "target_date": self._last_target_date,
                "routes": dict(self._routes),
                "avg_duration_seconds_last_n": round(self._durations.avg, 3),
            }

    def route_last_timestamps(self) -> Dict[str, str]:
        with self._lock:
            return {rc: meta["generated_at"] for rc, meta in self._routes.items()}

    def avg_duration_seconds(self) -> float:
        with self._lock:
            return round(self._durations.avg, 3)


# ---------------------------------------------------------------------------
# Module-level singletons (injected via dependencies)
# ---------------------------------------------------------------------------

_TRACKER: Optional[LastGenerationTracker] = None
_CSV_SINK: Optional[MetricsCsvSink] = None
_SINGLETON_LOCK = threading.Lock()


def get_last_generation_tracker() -> LastGenerationTracker:
    global _TRACKER
    with _SINGLETON_LOCK:
        if _TRACKER is None:
            _TRACKER = LastGenerationTracker()
        return _TRACKER


def get_metrics_csv_sink(
    shared_data_dir: str, clamps: Optional[SafetyClamps] = None,
) -> MetricsCsvSink:
    global _CSV_SINK
    c = clamps or SafetyClamps()
    with _SINGLETON_LOCK:
        if _CSV_SINK is None:
            _CSV_SINK = MetricsCsvSink(
                path=Path(shared_data_dir) / "generation_metrics.csv",
                max_bytes=c.generation_metrics_max_bytes,
            )
        return _CSV_SINK


def log_gen_metrics_line(
    route_code: str,
    gen: str,
    candidates: int,
    kept: int,
    extras: Optional[Dict[str, Any]] = None,
) -> None:
    """Emit the ``gen_metrics`` key=value log line parsed by dashboards."""
    parts = [
        f"gen_metrics route={route_code}",
        f"gen={gen}",
        f"candidates={candidates}",
        f"kept={kept}",
    ]
    if extras:
        for k, v in extras.items():
            parts.append(f"{k}={v}")
    logger.info(" ".join(parts))
