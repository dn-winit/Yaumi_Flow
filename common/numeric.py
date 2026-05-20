"""Shared numeric coercion helpers.

Production code repeatedly needs to convert "something that came from a
DataFrame cell, a DB row, or a JSON field" into a clean numeric value.
The naive idiom ``float(x or 0.0)`` looks safe but silently breaks when
``x`` is a pandas NaN -- NaN is *truthy* in Python, so the ``or`` branch
is never taken and the NaN survives into JSON as ``null``. Downstream
aggregators that do ``total += value`` then poison their running sums.

``safe_float`` is the single coercion point that neutralises:
  * ``None``                       -> ``default``
  * pandas NaN, ``float('nan')``   -> ``default`` (NaN check via ``v != v``)
  * positive / negative infinity   -> ``default`` (JSON does not represent these)
  * empty / non-numeric strings    -> ``default`` (``ValueError`` swallowed)

It is intentionally tolerant: every CSV column, DB row, and JSON cell
flows through it without raising. The cost is one branch and one NaN
check per call -- negligible against the ~1ms-per-call work that
follows in the per-route aggregators that use it.

Why a shared module: ``data_import.services.eda_service`` and
``demand_forecasting_pipeline.services.reconciliation.van_load_service``
both materialise per-item dicts from the same CSV mirrors. A divergence
between their coercion rules means the same source row can produce
different ``van_load`` values depending on which service rendered it,
which silently breaks the cross-view carry identities the reconciliation
verifier checks. Centralising the rule here keeps them in lock-step.
"""
from __future__ import annotations

from typing import Any


def safe_float(value: Any, default: float = 0.0) -> float:
    """Coerce ``value`` to a finite float, returning ``default`` for
    anything that can't be represented as one (None, NaN, inf,
    non-numeric strings).

    Use this instead of ``float(x or 0.0)`` whenever ``x`` may be a
    pandas NaN -- NaN is truthy, so the ``or`` short-circuit never
    fires and NaN propagates downstream.
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
    """Integer counterpart of ``safe_float``. Floats with fractional
    parts truncate toward zero (matches ``int(x)`` semantics)."""
    f = safe_float(value, default=float(default))
    return int(f)
