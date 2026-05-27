"""
In-memory cost ring buffer for the /cost endpoints.

Process-local + bounded by ``cost_buffer_size``. The structured ``llm_call``
log line in ``LLMClient`` remains the audit trail; this is the live
read-side view that the cost endpoints aggregate.

Thread safety: a single ``threading.Lock`` protects the deque + counters.
No nested locks, no callbacks held under the lock, so no deadlock surface.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class CostEntry:
    timestamp:     datetime
    artifact:      str           # pre_visit_briefing | customer_analysis | route_analysis
    model:         str
    provider:      str
    input_tokens:  int
    output_tokens: int
    elapsed_ms:    float
    outcome:       str           # ok | rate_limited | truncated | parse_error | refusal | ...
    cache_hit:     bool
    route_code:    str = ""
    customer_code: str = ""


class CostTracker:
    """Thread-safe ring buffer of LLM call records.

    Public surface is intentionally small: ``record`` to append, ``summary``
    to aggregate. Both are O(N) in entries-since-since but the buffer is
    bounded so each call is bounded.
    """

    def __init__(self, *, max_entries: int) -> None:
        self._lock = threading.Lock()
        self._buffer: Deque[CostEntry] = deque(maxlen=max_entries)

    def record(self, entry: CostEntry) -> None:
        with self._lock:
            self._buffer.append(entry)

    def _snapshot(self) -> List[CostEntry]:
        # Snapshot under lock, aggregate outside. Holds the lock only for
        # the deque copy (microseconds) so a concurrent ``record`` never waits.
        with self._lock:
            return list(self._buffer)

    def summary(
        self,
        *,
        price_input_usd_per_million:  float,
        price_output_usd_per_million: float,
        display_currency: str,
        display_rate: float,
        since: Optional[datetime] = None,
        route_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Aggregate the buffer into a JSON-shaped report.

        ``since`` defaults to start-of-today (UTC) so the headline number
        answers "what did we spend today?". ``route_code`` filters to one
        route. Both unfiltered totals and per-artifact + per-outcome
        breakdowns are returned so the UI can drill in without re-querying.
        """
        cutoff = since or datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        rows = [
            e for e in self._snapshot()
            if e.timestamp >= cutoff
            and (route_code is None or e.route_code == route_code)
        ]

        total_calls         = len(rows)
        ok_calls            = sum(1 for e in rows if e.outcome == "ok")
        cache_hits          = sum(1 for e in rows if e.cache_hit)
        total_input_tokens  = sum(e.input_tokens  for e in rows)
        total_output_tokens = sum(e.output_tokens for e in rows)
        total_elapsed_ms    = sum(e.elapsed_ms    for e in rows)

        usd_cost = (
            total_input_tokens  * price_input_usd_per_million  / 1_000_000.0
          + total_output_tokens * price_output_usd_per_million / 1_000_000.0
        )

        per_artifact: Dict[str, Dict[str, Any]] = {}
        per_outcome:  Dict[str, int] = {}
        for e in rows:
            a = per_artifact.setdefault(e.artifact, {
                "calls": 0, "input_tokens": 0, "output_tokens": 0,
                "elapsed_ms": 0.0, "cache_hits": 0,
            })
            a["calls"]         += 1
            a["input_tokens"]  += e.input_tokens
            a["output_tokens"] += e.output_tokens
            a["elapsed_ms"]    += e.elapsed_ms
            if e.cache_hit:
                a["cache_hits"] += 1
            per_outcome[e.outcome] = per_outcome.get(e.outcome, 0) + 1

        return {
            "since":               cutoff.isoformat(),
            "route_code":          route_code,
            "total_calls":         total_calls,
            "ok_calls":            ok_calls,
            "degraded_calls":      total_calls - ok_calls,
            "cache_hits":          cache_hits,
            "cache_hit_rate":      round(cache_hits / max(total_calls, 1), 4),
            "total_input_tokens":  total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens":        total_input_tokens + total_output_tokens,
            "total_elapsed_ms":    round(total_elapsed_ms, 1),
            "avg_elapsed_ms":      round(total_elapsed_ms / max(total_calls, 1), 1),
            "cost_usd":            round(usd_cost, 6),
            "cost_display":        round(usd_cost * display_rate, 4),
            "display_currency":    display_currency,
            "display_rate":        display_rate,
            "per_artifact":        per_artifact,
            "per_outcome":         per_outcome,
        }


# Process-singleton instance, lazily sized on first import. The size is read
# from settings at construction so an env override applies cleanly on restart.
_INSTANCE: Optional[CostTracker] = None
_INIT_LOCK = threading.Lock()


def get_cost_tracker() -> CostTracker:
    """Return the process-wide tracker, constructing it on first call."""
    global _INSTANCE
    if _INSTANCE is not None:
        return _INSTANCE
    with _INIT_LOCK:
        if _INSTANCE is None:
            from llm_analytics.config.settings import get_settings
            _INSTANCE = CostTracker(max_entries=get_settings().cost_buffer_size)
    return _INSTANCE
