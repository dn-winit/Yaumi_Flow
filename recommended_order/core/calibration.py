"""
Per-route calibration.

All thresholds that used to be hardcoded magic numbers (0.75 completion gate,
90d dormancy, 50-unit qty benchmark, 75/55/35/15 tier cuts, ...) are derived
here from the observed distribution of sales on the route. Safety clamps
live in ``SafetyClamps`` -- they are the only numeric constants allowed in
business code.

Sprint-3 additions:

* **Rolling window**: ``calibrate()`` accepts ``window_days`` (the user thinks of
  this as "last N days of behaviour") and filters ``customer_df`` to the
  trailing window before computing percentiles. Too-little-signal windows
  fall back to corpus-wide calibration with ``fallback=True``.
* **Anti-overfit sanity clamp** (``_sanity_clamp``): a per-route field that is
  more than ``sanity_clamp_zscore`` stdevs from the corpus distribution of
  that field gets clamped to the ``sanity_clamp_percentile`` of corpus values
  (and a warning is logged).
* **Feedback plumbing**: ``RouteCalibration.source_weight_adjustments`` carries
  per-source multipliers derived by ``core.feedback``.
* **Cache safety**: LRU + TTL (bounds: ``SafetyClamps.cache_max_entries`` /
  ``cache_ttl_seconds``). Previously unbounded.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import numpy as np
import pandas as pd

from recommended_order.config.constants import SafetyClamps
from recommended_order.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RouteCalibration:
    """Per-route thresholds derived from observed sales.

    The ``derived_from`` dict documents how each field was computed, including
    (Sprint-3) the rolling-window date range actually used and a ``fallback``
    flag when the window had insufficient signal.
    """

    route_code: str

    # Item / customer filters
    frequency_floor: float            # P25 of per-customer item frequencies
    dormancy_days: int                # P95 of max inter-visit gap (clamped)
    qty_benchmark: float              # P90 of avg item qty per visit

    # Scoring / tiering
    tier_cuts: Dict[str, float]       # {"must_stock", "should_stock", "consider", "monitor"}
    completion_gate: float            # lowest completion ratio that opens scoring
    basket_min_confidence: float      # min P(B|A) for basket complements (P60 of route)
    recency_half_life_days: float     # exp-decay weighting

    # Lookalike-peer cross-sell (Sprint-2)
    peer_lookalike_k: int             # top-K similar customers used per target
    peer_lookalike_floor: float       # min similarity-weighted score to emit
    peer_max_per_customer: int        # max peer recs emitted per customer

    # Adaptive priority weights (low-freq vs high-freq items)
    priority_weights_low_freq: Dict[str, float]
    priority_weights_high_freq: Dict[str, float]

    # Sprint-3: per-source multipliers from the adaptive feedback loop.
    # Keys: "history", "basket", "peer". Missing keys default to 1.0 at use sites.
    source_weight_adjustments: Dict[str, float] = field(default_factory=dict)

    # Sprint-4: per-source sample-size confidence in the multiplier
    # (min(1, n / k)). 0.0 when no attributed samples; 1.0 when the route
    # has at least as many samples as the prior strength k.
    feedback_confidence: Dict[str, float] = field(default_factory=dict)

    # Traceability -- every number above has an entry here; Sprint-3 adds:
    #   derived_from["window"] = {"days", "start", "end", "fallback": bool}
    derived_from: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# LRU + TTL cache
# ---------------------------------------------------------------------------

_LOCK = threading.Lock()
# key: (route_code, csv_mtime, window_days) -> (inserted_at, calib)
_CACHE: "OrderedDict[Tuple[str, float, int], Tuple[float, RouteCalibration]]" = OrderedDict()


def _cache_get(key: Tuple[str, float, int], ttl: int) -> Optional[RouteCalibration]:
    with _LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        inserted, calib = entry
        if time.time() - inserted > ttl:
            _CACHE.pop(key, None)
            return None
        # LRU bump
        _CACHE.move_to_end(key)
        return calib


def _cache_put(
    key: Tuple[str, float, int], calib: RouteCalibration, max_entries: int,
) -> None:
    with _LOCK:
        _CACHE[key] = (time.time(), calib)
        _CACHE.move_to_end(key)
        while len(_CACHE) > max_entries:
            _CACHE.popitem(last=False)  # evict oldest


def invalidate_cache() -> None:
    """Drop all cached calibrations (called after CSV refresh)."""
    with _LOCK:
        _CACHE.clear()


def cache_size() -> int:
    with _LOCK:
        return len(_CACHE)


def _csv_mtime(settings: Optional[Settings] = None) -> float:
    """Max mtime of the source CSVs -- any refresh invalidates cache."""
    s = settings or get_settings()
    d = Path(s.shared_data_dir)
    files = [d / s.customer_data_file, d / s.demand_forecast_file, d / s.journey_plan_file]
    mtimes = [p.stat().st_mtime for p in files if p.exists()]
    return max(mtimes) if mtimes else 0.0


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

def calibrate(
    customer_df: pd.DataFrame,
    demand_df: pd.DataFrame,
    route_code: str,
    *,
    clamps: Optional[SafetyClamps] = None,
    settings: Optional[Settings] = None,
    corpus_median_customers: Optional[float] = None,
    window_days: Optional[int] = None,
    corpus_field_values: Optional[Dict[str, Iterable[float]]] = None,
    source_weight_adjustments: Optional[Dict[str, float]] = None,
    feedback_confidence: Optional[Dict[str, float]] = None,
) -> RouteCalibration:
    """Return a cached ``RouteCalibration`` for ``route_code``.

    Args:
        window_days: Trailing window (days) of ``customer_df`` to calibrate from.
            The user's mental model is "the last N days of behaviour". When the
            window yields fewer than ``calibration_fallback_min_days`` distinct
            dates OR fewer than ``calibration_fallback_min_customers`` customers,
            we fall back to the full corpus-wide frame and record
            ``derived_from["window"]["fallback"] = True``.
        corpus_field_values: Optional corpus-wide distributions of each
            calibration field for the anti-overfit sanity clamp. When provided,
            per-route values farther than ``sanity_clamp_zscore`` stdevs from
            the corpus mean are clamped to the ``sanity_clamp_percentile``.
        source_weight_adjustments: Per-source multipliers from the feedback loop.
            Passed through to ``RouteCalibration``; multiplicative down-weighting
            is applied at merge time in the engine.

    Cache key: (route, csv_mtime, window_days). LRU + TTL bounded by
    ``SafetyClamps``.
    """
    c = clamps or SafetyClamps()
    w = int(window_days if window_days is not None else c.calibration_window_days)
    # Honour the named bounds
    w = max(c.calibration_window_min_days, min(c.calibration_window_max_days, w))

    key = (route_code, _csv_mtime(settings), w)
    cached = _cache_get(key, c.cache_ttl_seconds)
    # If feedback adjustments/confidence were provided, we must refresh --
    # cache hits return unmodified entries so re-cache with the new multipliers.
    if cached is not None and not source_weight_adjustments and not feedback_confidence:
        return cached

    calib = _compute(
        customer_df, demand_df, route_code, c,
        window_days=w,
        corpus_median_customers=corpus_median_customers,
        corpus_field_values=corpus_field_values,
        source_weight_adjustments=source_weight_adjustments or {},
        feedback_confidence=feedback_confidence or {},
    )
    _cache_put(key, calib, c.cache_max_entries)
    return calib


# ---------------------------------------------------------------------------
# Window + fallback
# ---------------------------------------------------------------------------

def _window_filter(
    customer_df: pd.DataFrame, window_days: int, clamps: SafetyClamps,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Filter ``customer_df`` to the trailing ``window_days`` from its max date.

    Returns the filtered frame + a ``window`` meta dict. If the window is
    degenerate (too few days or customers), we return the *unfiltered* frame
    with ``fallback=True`` and the range of the unfiltered frame.
    """
    if customer_df.empty or "TrxDate" not in customer_df.columns:
        return customer_df, {
            "days": int(window_days),
            "start": None,
            "end": None,
            "fallback": True,
            "reason": "empty or missing TrxDate",
        }

    dates = pd.to_datetime(customer_df["TrxDate"], errors="coerce")
    if dates.isna().all():
        return customer_df, {
            "days": int(window_days),
            "start": None,
            "end": None,
            "fallback": True,
            "reason": "no parseable dates",
        }

    end = dates.max().normalize()
    start = end - timedelta(days=int(window_days))
    mask = dates >= start
    filtered = customer_df.loc[mask]

    distinct_days = int(pd.to_datetime(filtered["TrxDate"]).dt.normalize().nunique())
    distinct_customers = int(filtered["CustomerCode"].nunique()) if not filtered.empty else 0

    if (
        distinct_days < clamps.calibration_fallback_min_days
        or distinct_customers < clamps.calibration_fallback_min_customers
    ):
        full_dates = dates.dropna()
        return customer_df, {
            "days": int(window_days),
            "start": str(full_dates.min().date()),
            "end": str(end.date()),
            "fallback": True,
            "reason": (
                f"window has {distinct_days} days / {distinct_customers} customers; "
                f"below thresholds ({clamps.calibration_fallback_min_days} / "
                f"{clamps.calibration_fallback_min_customers})"
            ),
        }
    return filtered, {
        "days": int(window_days),
        "start": str(start.date()),
        "end": str(end.date()),
        "fallback": False,
    }


# ---------------------------------------------------------------------------
# Anti-overfit sanity clamp
# ---------------------------------------------------------------------------

def _sanity_clamp(
    route_value: float,
    corpus_distribution: Iterable[float],
    field_name: str,
    *,
    clamps: SafetyClamps,
) -> Tuple[float, Dict[str, Any]]:
    """Clamp ``route_value`` to the corpus percentile when it's an outlier.

    If |route_value - corpus_mean| / corpus_std > ``sanity_clamp_zscore``, the
    route value is pulled to the ``sanity_clamp_percentile`` of the corpus
    distribution and a warning is logged. Returns ``(clamped_value, meta)``.
    Small or degenerate corpora are a no-op.
    """
    arr = np.asarray([float(v) for v in corpus_distribution if v is not None], dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < 5:
        return float(route_value), {"applied": False, "reason": "corpus too small"}
    mean = float(arr.mean())
    std = float(arr.std(ddof=0))
    if std <= 0:
        return float(route_value), {"applied": False, "reason": "corpus std==0"}
    z = abs(float(route_value) - mean) / std
    if z <= clamps.sanity_clamp_zscore:
        return float(route_value), {"applied": False, "zscore": round(z, 2)}
    clamp_to = float(np.percentile(arr, clamps.sanity_clamp_percentile))
    logger.warning(
        "Sanity clamp: field=%s route_value=%.4f zscore=%.2f -> clamped to P%.0f=%.4f",
        field_name, float(route_value), z, clamps.sanity_clamp_percentile, clamp_to,
    )
    return clamp_to, {
        "applied": True,
        "zscore": round(z, 2),
        "corpus_mean": round(mean, 4),
        "corpus_std": round(std, 4),
        "clamped_to": round(clamp_to, 4),
    }


# ---------------------------------------------------------------------------
# Computation
# ---------------------------------------------------------------------------

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _compute(
    customer_df: pd.DataFrame,
    demand_df: pd.DataFrame,
    route_code: str,
    c: SafetyClamps,
    *,
    window_days: int,
    corpus_median_customers: Optional[float] = None,
    corpus_field_values: Optional[Dict[str, Iterable[float]]] = None,
    source_weight_adjustments: Optional[Dict[str, float]] = None,
    feedback_confidence: Optional[Dict[str, float]] = None,
) -> RouteCalibration:
    derived: Dict[str, Any] = {}

    # Sprint-3: apply the rolling window up-front. Everything below uses windowed.
    windowed, window_meta = _window_filter(customer_df, window_days, c)
    derived["window"] = window_meta

    num_active_customers = (
        int(windowed["CustomerCode"].nunique()) if not windowed.empty else 0
    )

    # ---- frequency_floor: P25 of per-customer item buy frequencies ----
    if windowed.empty:
        frequency_floor = c.freq_floor_min
        derived["frequency_floor"] = "empty customer_df -> clamp min"
    else:
        visits_per_cust = windowed.groupby("CustomerCode")["TrxDate"].nunique()
        item_visits = windowed.groupby(["CustomerCode", "ItemCode"])["TrxDate"].nunique()
        freq = (item_visits / visits_per_cust.reindex(item_visits.index.get_level_values(0)).values)
        freq = freq.replace([np.inf, -np.inf], np.nan).dropna()
        if freq.empty:
            frequency_floor = c.freq_floor_min
            derived["frequency_floor"] = "no frequencies -> clamp min"
        else:
            raw = float(np.percentile(freq, 25))
            sparsity_factor = 1.0
            if corpus_median_customers and corpus_median_customers > 0:
                ratio = num_active_customers / corpus_median_customers
                if ratio < c.sparse_route_customer_ratio:
                    t = max(0.0, ratio / c.sparse_route_customer_ratio)
                    sparsity_factor = (
                        c.sparse_route_freq_floor_relief
                        + (1.0 - c.sparse_route_freq_floor_relief) * t
                    )
            adjusted = raw * sparsity_factor
            frequency_floor = _clamp(adjusted, c.freq_floor_min, c.freq_floor_max)
            derived["frequency_floor"] = {
                "method": "P25 of per-customer item frequencies x sparsity_factor",
                "raw": round(raw, 4),
                "sparsity_factor": round(sparsity_factor, 4),
                "active_customers": num_active_customers,
                "corpus_median_customers": corpus_median_customers,
                "clamped": round(frequency_floor, 4),
                "n": int(len(freq)),
            }

    # ---- dormancy_days: P95 of max inter-visit gap per customer ----
    if windowed.empty:
        dormancy_days = c.dormancy_min_days
        derived["dormancy_days"] = "empty -> clamp min"
    else:
        gaps = []
        for _, g in windowed.groupby("CustomerCode"):
            dates = pd.to_datetime(g["TrxDate"]).sort_values().unique()
            if len(dates) >= 2:
                diffs = np.diff(dates).astype("timedelta64[D]").astype(int)
                gaps.append(int(diffs.max()))
        if not gaps:
            dormancy_days = c.dormancy_min_days
            derived["dormancy_days"] = "no gap data -> clamp min"
        else:
            raw = float(np.percentile(gaps, 95))
            dormancy_days = int(_clamp(raw, c.dormancy_min_days, c.dormancy_max_days))
            derived["dormancy_days"] = {
                "method": "P95 of per-customer max inter-visit gap",
                "raw": round(raw, 1),
                "clamped": dormancy_days,
                "n": len(gaps),
            }

    # ---- qty_benchmark: P90 of avg item qty per visit ----
    if windowed.empty or "TotalQuantity" not in windowed.columns:
        qty_benchmark = c.qty_benchmark_min
        derived["qty_benchmark"] = "empty -> clamp min"
    else:
        avg_qty_per_item = windowed.groupby("ItemCode")["TotalQuantity"].mean().dropna()
        if avg_qty_per_item.empty:
            qty_benchmark = c.qty_benchmark_min
            derived["qty_benchmark"] = "no qty data -> clamp min"
        else:
            raw = float(np.percentile(avg_qty_per_item, 90))
            qty_benchmark = _clamp(raw, c.qty_benchmark_min, c.qty_benchmark_max)
            derived["qty_benchmark"] = {
                "method": "P90 of per-item avg qty",
                "raw": round(raw, 2),
                "clamped": round(qty_benchmark, 2),
            }

    # ---- priority score distribution -> tier cuts ----
    tier_cuts = _tier_cuts(windowed, c, derived)

    # ---- completion_gate: min(cap, 1 - 1/median_cycle) ----
    median_cycle = _median_cycle_days(windowed)
    if median_cycle <= 1:
        completion_gate = c.completion_gate_cap
        derived["completion_gate"] = f"median_cycle={median_cycle} -> clamp to cap"
    else:
        raw = 1.0 - (1.0 / median_cycle)
        completion_gate = _clamp(raw, c.completion_gate_floor, c.completion_gate_cap)
        derived["completion_gate"] = {
            "method": "1 - 1/median_cycle_days",
            "median_cycle_days": median_cycle,
            "raw": round(raw, 4),
            "clamped": round(completion_gate, 4),
        }

    # ---- basket_min_confidence ----
    basket_min_confidence, basket_derivation = _basket_min_confidence(windowed, c)
    derived["basket_min_confidence"] = basket_derivation

    # ---- peer_lookalike_k: sqrt(n_active_customers), clamped ----
    raw_k = float(np.sqrt(max(1, num_active_customers)))
    peer_lookalike_k = int(_clamp(raw_k, c.peer_lookalike_k_min, c.peer_lookalike_k_max))
    derived["peer_lookalike_k"] = {
        "method": "clamp(sqrt(num_active_customers_on_route), [k_min, k_max])",
        "num_active_customers": num_active_customers,
        "raw": round(raw_k, 2),
        "clamped": peer_lookalike_k,
    }

    # ---- peer_lookalike_floor ----
    peer_lookalike_floor = c.peer_lookalike_floor_min
    derived["peer_lookalike_floor"] = {
        "method": "min of clamp band; generator lifts to P75 of observed scores",
        "clamp": [c.peer_lookalike_floor_min, c.peer_lookalike_floor_max],
    }

    # ---- peer_max_per_customer ----
    raw_max = float(qty_benchmark) / max(1.0, c.peer_max_per_customer_qty_divisor)
    peer_max_per_customer = int(_clamp(
        raw_max, c.peer_max_per_customer_min, c.peer_max_per_customer_max,
    ))
    derived["peer_max_per_customer"] = {
        "method": "clamp(qty_benchmark / divisor, [min, max])",
        "qty_benchmark": round(float(qty_benchmark), 2),
        "divisor": c.peer_max_per_customer_qty_divisor,
        "raw": round(raw_max, 2),
        "clamped": peer_max_per_customer,
    }

    # ---- recency_half_life_days ----
    if median_cycle <= 0:
        recency_half_life_days = float(c.recency_half_life_min)
        derived["recency_half_life_days"] = "median_cycle<=0 -> clamp min"
    else:
        raw = median_cycle * 3.0
        recency_half_life_days = _clamp(
            raw, c.recency_half_life_min, c.recency_half_life_max
        )
        derived["recency_half_life_days"] = {
            "method": "3 x median_inter_visit_gap",
            "raw": round(raw, 1),
            "clamped": round(recency_half_life_days, 1),
        }

    # ---- priority weights (low/high freq) ----
    priority_weights_low_freq = {
        "timing": 0.30,
        "quantity": 0.40,
        "consistency": 0.30,
    }
    priority_weights_high_freq = {
        "timing": 0.55,
        "quantity": 0.25,
        "consistency": 0.20,
    }
    derived["priority_weights"] = {
        "low_freq": priority_weights_low_freq,
        "high_freq": priority_weights_high_freq,
        "blending": "linear in frequency",
    }

    # ---- Sprint-3: anti-overfit sanity clamp against corpus distributions ----
    sanity_meta: Dict[str, Any] = {}
    if corpus_field_values:
        for name, current in (
            ("frequency_floor", frequency_floor),
            ("dormancy_days", dormancy_days),
            ("qty_benchmark", qty_benchmark),
            ("completion_gate", completion_gate),
            ("basket_min_confidence", basket_min_confidence),
            ("recency_half_life_days", recency_half_life_days),
        ):
            corpus_vals = corpus_field_values.get(name)
            if corpus_vals is None:
                continue
            new_val, meta = _sanity_clamp(
                float(current), corpus_vals, name, clamps=c,
            )
            if meta.get("applied"):
                sanity_meta[name] = meta
                if name == "frequency_floor":
                    frequency_floor = float(new_val)
                elif name == "dormancy_days":
                    dormancy_days = int(round(new_val))
                elif name == "qty_benchmark":
                    qty_benchmark = float(new_val)
                elif name == "completion_gate":
                    completion_gate = float(new_val)
                elif name == "basket_min_confidence":
                    basket_min_confidence = float(new_val)
                elif name == "recency_half_life_days":
                    recency_half_life_days = float(new_val)
    derived["sanity_clamps"] = sanity_meta or {"applied_to": []}

    return RouteCalibration(
        route_code=route_code,
        frequency_floor=frequency_floor,
        dormancy_days=dormancy_days,
        qty_benchmark=float(qty_benchmark),
        tier_cuts=tier_cuts,
        completion_gate=completion_gate,
        basket_min_confidence=basket_min_confidence,
        recency_half_life_days=recency_half_life_days,
        peer_lookalike_k=peer_lookalike_k,
        peer_lookalike_floor=peer_lookalike_floor,
        peer_max_per_customer=peer_max_per_customer,
        priority_weights_low_freq=priority_weights_low_freq,
        priority_weights_high_freq=priority_weights_high_freq,
        source_weight_adjustments=dict(source_weight_adjustments or {}),
        feedback_confidence=dict(feedback_confidence or {}),
        derived_from=derived,
    )


def _basket_min_confidence(
    customer_df: pd.DataFrame, c: SafetyClamps,
) -> Tuple[float, Any]:
    """P60 of observed per-route co-occurrence confidences, clamped."""
    if customer_df.empty:
        return (
            _clamp(0.5, c.basket_min_confidence_min, c.basket_min_confidence_max),
            "empty -> midpoint",
        )
    cust_items: Dict[str, set] = {}
    for (cust, item) in customer_df[["CustomerCode", "ItemCode"]].itertuples(index=False):
        cust_items.setdefault(str(cust), set()).add(str(item))
    item_customers: Dict[str, set] = {}
    for cust, items in cust_items.items():
        for it in items:
            item_customers.setdefault(it, set()).add(cust)

    confidences: list[float] = []
    for a, a_customers in item_customers.items():
        if len(a_customers) < 3:
            continue
        for b, b_customers in item_customers.items():
            if a == b:
                continue
            both = a_customers & b_customers
            if len(both) < 2:
                continue
            confidences.append(len(both) / len(a_customers))
    if not confidences:
        clamped = _clamp(
            0.5, c.basket_min_confidence_min, c.basket_min_confidence_max,
        )
        return clamped, "no co-occurrences -> midpoint"
    raw = float(np.percentile(confidences, 60))
    clamped = _clamp(raw, c.basket_min_confidence_min, c.basket_min_confidence_max)
    return clamped, {
        "method": "P60 of observed co-occurrence confidences",
        "raw": round(raw, 4),
        "clamped": round(clamped, 4),
        "n_pairs": len(confidences),
    }


def _tier_cuts(
    customer_df: pd.DataFrame, c: SafetyClamps, derived: Dict[str, Any]
) -> Dict[str, float]:
    """Quantile-based tier thresholds (0-100 priority scale)."""
    if customer_df.empty:
        cuts = {
            "must_stock": c.tier_must_stock_min,
            "should_stock": c.tier_should_stock_min,
            "consider": c.tier_consider_min,
            "monitor": c.tier_monitor_min,
        }
        derived["tier_cuts"] = "empty -> safety-clamp floors"
        return cuts

    counts = customer_df.groupby(["CustomerCode", "ItemCode"]).size()
    if counts.empty:
        cuts = {
            "must_stock": c.tier_must_stock_min,
            "should_stock": c.tier_should_stock_min,
            "consider": c.tier_consider_min,
            "monitor": c.tier_monitor_min,
        }
        derived["tier_cuts"] = "no counts -> floors"
        return cuts

    ranks = counts.rank(pct=True) * 100
    p25 = float(np.percentile(ranks, 25))
    p50 = float(np.percentile(ranks, 50))
    p75 = float(np.percentile(ranks, 75))
    p90 = float(np.percentile(ranks, 90))

    cuts = {
        "must_stock": _clamp(p90, c.tier_must_stock_min, c.tier_must_stock_max),
        "should_stock": _clamp(p75, c.tier_should_stock_min, c.tier_should_stock_max),
        "consider": _clamp(p50, c.tier_consider_min, c.tier_consider_max),
        "monitor": _clamp(p25, c.tier_monitor_min, c.tier_monitor_max),
    }
    derived["tier_cuts"] = {
        "method": "quantiles (P90, P75, P50, P25) of rank-percentile score distribution",
        "raw": {"p90": round(p90, 1), "p75": round(p75, 1), "p50": round(p50, 1), "p25": round(p25, 1)},
        "clamped": {k: round(v, 1) for k, v in cuts.items()},
    }
    return cuts


def _median_cycle_days(customer_df: pd.DataFrame) -> int:
    """Median inter-visit gap across all customers (days)."""
    if customer_df.empty:
        return 0
    gaps: list[int] = []
    for _, g in customer_df.groupby("CustomerCode"):
        dates = pd.to_datetime(g["TrxDate"]).sort_values().unique()
        if len(dates) >= 2:
            diffs = np.diff(dates).astype("timedelta64[D]").astype(int)
            gaps.extend(int(d) for d in diffs if d > 0)
    if not gaps:
        return 0
    return int(np.median(gaps))


def classify_tier(score: float, cuts: Dict[str, float]) -> str:
    """Classify a 0-100 priority score using calibrated cuts."""
    if score >= cuts["must_stock"]:
        return "MUST_STOCK"
    if score >= cuts["should_stock"]:
        return "SHOULD_STOCK"
    if score >= cuts["consider"]:
        return "CONSIDER"
    if score >= cuts["monitor"]:
        return "MONITOR"
    return "EXCLUDE"
