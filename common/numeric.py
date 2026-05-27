"""Shared numeric coercion + presentation rounding helpers.

Two responsibilities: (1) ``safe_float`` / ``safe_int`` neutralise
NaN/None/inf from noisy upstream data, (2) ``pack_qty`` / ``currency`` /
``pct`` / ``prob`` are the SINGLE rounding policy at the API->client
boundary so tile / table / chart sums reconcile by construction. All
are NaN/None-safe and JSON-serialisable.
"""
from __future__ import annotations

import math
from typing import Any


def safe_float(value: Any, default: float = 0.0) -> float:
    """Coerce ``value`` to a finite float; return ``default`` for None,
    NaN, inf, or non-numeric strings. Prefer over ``float(x or 0.0)``
    because pandas NaN is truthy and bypasses the ``or`` short-circuit.
    """
    if value is None:
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    # NaN != NaN; inf is finite-by-IEEE but not JSON-representable.
    if v != v or v == float("inf") or v == float("-inf"):
        return default
    return v


def safe_int(value: Any, default: int = 0) -> int:
    """Integer counterpart of ``safe_float`` (truncates toward zero)."""
    f = safe_float(value, default=float(default))
    return int(f)


# Presentation rounding: apply each helper exactly ONCE per value at the
# API output layer so per-row, per-day and headline aggregates reconcile.

def pack_qty(value: Any) -> int:
    """Quantity as whole truck packs (ceil-up integer; 0 if non-positive).
    Apply cell-by-cell so ``tile_total == sum(table_column)`` by construction;
    do not re-apply to aggregates.
    """
    f = safe_float(value, default=0.0)
    if f <= 0.0:
        return 0
    return int(math.ceil(f))


def currency(value: Any, digits: int = 2) -> float:
    """AED amount rounded to ``digits`` decimals (default 2). Aggregate raw
    floats internally; apply once to the final aggregate for display."""
    return round(safe_float(value, default=0.0), digits)


def pct(value: Any, digits: int = 1) -> float:
    """Percentage already on the 0-100 scale, rounded to ``digits`` (default 1).
    Does NOT multiply by 100."""
    return round(safe_float(value, default=0.0), digits)


def prob(value: Any) -> float:
    """Probability in [0, 1] rounded to 2dp (engine's ``p_demand`` display).
    NaN clamps to 0.0."""
    return round(safe_float(value, default=0.0), 2)
