"""Candidate generators (one scoring lane each, pure functions).

Order: history -> peer_cross_sell -> basket_complement -> reactivation -> seed.
Engine de-dupes and ranks across lanes.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from recommended_order.config.constants import SafetyClamps, UniversalFilters
from recommended_order.core.calibration import RouteCalibration
from recommended_order.core.cycle import CycleCalculator
from recommended_order.core.explain import (
    KIND_BASKET_COMPLEMENT,
    KIND_FIRST_VISIT,
    KIND_LOOKALIKE_PEER,
    KIND_REACTIVATION,
    KIND_TRENDING_DOWN,
    KIND_TRENDING_UP,
    Explanation,
    Signal,
    detail_basket_complement,
    detail_first_visit,
    detail_lookalike_peer,
    detail_qty_basket,
    detail_qty_peer,
    detail_qty_seed,
    detail_reactivation,
    detail_trending_down,
    detail_trending_up,
)
from recommended_order.core.priority import PriorityCalculator
from recommended_order.core.quantity import QuantityCalculator
from recommended_order.core.trend import TrendCalculator
from recommended_order.models.recommendation import Candidate


def _finalize(cand: Candidate, expl: Explanation) -> Candidate:
    cand.signals = expl.signals()
    cand.why_item = expl.why_item()
    cand.why_quantity = expl.why_quantity()
    cand.confidence = expl.confidence()
    return cand


# gen_history: customer's own purchase patterns.

def gen_history(
    customer: str,
    cust_history: pd.DataFrame,
    item_dict: dict[str, pd.DataFrame],
    van_items: dict[str, int],
    target_dt: pd.Timestamp,
    *,
    calibration: RouteCalibration,
    universal: UniversalFilters,
    clamps: SafetyClamps,
    cycle_calc: CycleCalculator,
    priority_calc: PriorityCalculator,
    quantity_calc: QuantityCalculator,
    trend_calc: TrendCalculator,
    profile: dict[str, Any] | None = None,
) -> list[Candidate]:
    out: list[Candidate] = []
    total_visits = int(cust_history["TrxDate"].nunique()) if not cust_history.empty else 0
    if total_visits == 0:
        return out

    # Customer-personalised gate / half-life / tier (with route-level fallback
    # baked in upstream). Resolved once outside the loop so per-item iterations
    # don't re-look-up the same scalars.
    if profile:
        completion_gate = float(profile.get("completion_gate") or calibration.completion_gate)
        half_life = float(profile.get("half_life_days") or calibration.recency_half_life_days)
        tier = str(profile.get("tier") or "MEDIUM")
    else:
        completion_gate = calibration.completion_gate
        half_life = calibration.recency_half_life_days
        tier = "MEDIUM"

    for item, van_qty in van_items.items():
        if van_qty <= 0 or item not in item_dict:
            continue
        hist = item_dict[item]
        if hist.empty:
            continue

        # TrxDate is already datetime64 (normalised at load) -- no re-parse.
        # Compute item_visits once and reuse for both the freq gate and the
        # downstream PriorityCalculator (avoids a second nunique() pass).
        item_dates = hist["TrxDate"]
        last_purchase = item_dates.max()
        days_since = (target_dt - last_purchase).days
        if days_since <= universal.min_days_since_purchase:
            continue

        item_visits = int(item_dates.nunique())
        frequency = item_visits / total_visits if total_visits > 0 else 0.0
        if frequency < calibration.frequency_floor:
            continue

        purchase_count = len(hist)
        cycle_info = cycle_calc.calculate(hist, target_dt)
        cycle_days = max(1, cycle_info.cycle_days)
        completion = days_since / cycle_days
        if completion < completion_gate:
            continue

        expl = Explanation()
        pri = priority_calc.calculate(
            hist, cust_history, target_dt,
            cycle_days=cycle_days, days_since=days_since,
            item_frequency=frequency, calibration=calibration,
            explanation=expl,
            tier=tier,
            total_visits=total_visits,
            item_visits=item_visits,
        )

        trend = trend_calc.calculate(hist, target_dt)
        # Match LOW_CONF variants too -- they're real trend signals, just
        # damped at the factor level. The signal/explanation strength is
        # taken from the factor (above/below 1.0), so the LOW_CONF cases
        # naturally produce milder explanations.
        ttype = trend.trend_type
        if ttype.startswith("ACCELERATING"):
            meta = trend.metadata or {}
            expl.add_item_signal(Signal(
                kind=KIND_TRENDING_UP,
                detail=detail_trending_up(
                    int(meta.get("historical_cycle", cycle_days)),
                    int(meta.get("recent_cycle", cycle_days)),
                ),
                weight=0.4,
                evidence=meta,
            ))
        elif ttype.startswith("DECLINING"):
            meta = trend.metadata or {}
            expl.add_item_signal(Signal(
                kind=KIND_TRENDING_DOWN,
                detail=detail_trending_down(
                    int(meta.get("historical_cycle", cycle_days)),
                    int(meta.get("recent_cycle", cycle_days)),
                ),
                weight=0.3,
                evidence=meta,
            ))

        score = pri.score * trend.factor
        # Minimum score gate = monitor-tier floor
        if score < calibration.tier_cuts["monitor"]:
            continue

        qty = quantity_calc.calculate(
            hist, target_dt, int(van_qty), float(trend.factor), calibration, expl,
            half_life_override=half_life,
        )
        if qty <= 0:
            continue

        pq, _ = cycle_calc.pattern_quality(hist)
        cand = Candidate(
            item_code=str(item),
            recommended_qty=qty,
            priority_score=round(score, 2),
            source="history",
            van_qty=int(van_qty),
            avg_qty=float(hist["TotalQuantity"].mean()),
            days_since=int(days_since),
            cycle_days=float(cycle_days),
            frequency_pct=round(frequency * 100, 1),
            pattern_quality=round(pq, 2),
            purchase_count=purchase_count,
            trend_factor=round(float(trend.factor), 2),
        )
        out.append(_finalize(cand, expl))
    return out


# gen_peer_cross_sell: top-K similar peers weight a candidate van item;
# similarity-weighted median qty across buyers; floor = max(calibration
# floor, P75 of observed scores).

def gen_peer_cross_sell(
    customer: str,
    item_dict: dict[str, pd.DataFrame],
    van_items: dict[str, int],
    *,
    lookalike_ctx: dict[str, Any],
    calibration: RouteCalibration,
    clamps: SafetyClamps,
    target_dt: pd.Timestamp | None = None,
    cycle_calc: CycleCalculator | None = None,
) -> list[Candidate]:
    cust_idx_map: dict[str, int] = lookalike_ctx.get("cust_idx", {})
    item_idx_map: dict[str, int] = lookalike_ctx.get("item_idx", {})
    if customer not in cust_idx_map or not item_idx_map:
        return []

    matrix: np.ndarray = lookalike_ctx["matrix"]
    similarity: np.ndarray = lookalike_ctx["similarity"]
    qty_matrix: np.ndarray = lookalike_ctx["qty_matrix"]

    i = cust_idx_map[customer]
    sims = similarity[i]
    if sims.size == 0 or float(sims.max()) <= 0.0:
        return []

    # Top-K similar peers with positive similarity
    k = max(1, int(calibration.peer_lookalike_k))
    top_k_idx = np.argsort(-sims)[:k]
    top_k_idx = top_k_idx[sims[top_k_idx] > 0]
    if top_k_idx.size == 0:
        return []
    top_sims = sims[top_k_idx]                          # (K,)
    peer_weights = matrix[top_k_idx]                    # (K, n_items)
    peer_qty = qty_matrix[top_k_idx]                    # (K, n_items)

    # Per-item similarity-weighted mean, normalised across actual buyers
    # only. Brand-new SKUs (no peer buyers) yield score=0 cleanly.
    bought_mask = peer_weights > 0                      # (K, n_items)
    sim_col = top_sims[:, None]                         # (K, 1)
    numer = (sim_col * peer_weights).sum(axis=0)        # (n_items,)
    denom = (sim_col * bought_mask).sum(axis=0)         # (n_items,)
    with np.errstate(invalid="ignore", divide="ignore"):
        scores = np.where(denom > 0, numer / denom, 0.0)

    # Don't drop items already in history: history outranks peer in
    # merge_and_rank, but the corroborating peer signal still attaches.

    # Restrict to van items that exist in the matrix
    van_mask = np.zeros_like(scores, dtype=bool)
    van_col_to_code: dict[int, str] = {}
    for item_code, van_qty in van_items.items():
        if int(van_qty or 0) <= 0:
            continue
        j = item_idx_map.get(str(item_code))
        if j is None:
            continue
        van_mask[j] = True
        van_col_to_code[j] = str(item_code)
    scores = np.where(van_mask, scores, 0.0)

    # Data-driven floor: max(calibration floor, P75 of positive candidate scores)
    positive = scores[scores > 0]
    if positive.size == 0:
        return []
    observed_p75 = float(np.percentile(positive, 75))
    floor = max(float(calibration.peer_lookalike_floor), observed_p75)
    floor = min(
        max(floor, float(clamps.peer_lookalike_floor_min)),
        float(clamps.peer_lookalike_floor_max),
    )

    eligible_cols = np.where(scores >= floor)[0]
    if eligible_cols.size == 0:
        return []

    # Rank by score desc, cap at peer_max_per_customer
    eligible_cols = sorted(eligible_cols, key=lambda c: -float(scores[c]))
    eligible_cols = eligible_cols[: int(calibration.peer_max_per_customer)]

    out: list[Candidate] = []
    for j in eligible_cols:
        item = van_col_to_code[int(j)]
        van_qty = int(van_items.get(item, 0))
        if van_qty <= 0:
            continue
        # Similarity-weighted median qty across peers who bought the item
        buyers_mask = bought_mask[:, j]
        if not buyers_mask.any():
            continue
        qtys = peer_qty[buyers_mask, j]
        weights = top_sims[buyers_mask]
        median_qty = float(_weighted_median(qtys, weights))
        if median_qty <= 0:
            continue
        # Ceil so a 17.4 median reads as 18.
        qty = min(van_qty, max(1, int(np.ceil(median_qty))))
        n_peer_buyers = int(buyers_mask.sum())
        score_val = float(scores[j])

        expl = Explanation()
        expl.add_item_signal(Signal(
            kind=KIND_LOOKALIKE_PEER,
            detail=detail_lookalike_peer(score_val * 100, n_peer_buyers),
            weight=min(1.0, score_val),
            evidence={
                "similarity_score": round(score_val, 4),
                "n_peers_considered": int(top_k_idx.size),
                "n_peer_buyers": n_peer_buyers,
                "floor": round(floor, 4),
            },
        ))
        expl.add_quantity_signal(Signal(
            kind="qty_derivation",
            detail=detail_qty_peer(median_qty, n_peer_buyers),
            weight=1.0,
            evidence={
                "peer_weighted_median_qty": round(median_qty, 2),
                "n_peer_buyers": n_peer_buyers,
                "van_cap": van_qty,
            },
        ))
        # Map score to priority band (consider <-> should_stock)
        pscore = calibration.tier_cuts["consider"] + min(1.0, score_val) * (
            calibration.tier_cuts["should_stock"] - calibration.tier_cuts["consider"]
        )
        # If the customer has bought this item, fill history fields
        # truthfully; else leave at 0 (purchase_count==0 is the canonical
        # "no personal history" contract).
        own_hist = item_dict.get(item)
        if own_hist is not None and not own_hist.empty:
            own_purchase_count = int(len(own_hist))
            own_dates = own_hist["TrxDate"]  # already datetime64
            if target_dt is not None:
                own_days_since = int((target_dt - own_dates.max()).days)
            else:
                own_days_since = 0
            if cycle_calc is not None and target_dt is not None:
                own_cycle = float(cycle_calc.calculate(own_hist, target_dt).cycle_days)
            else:
                own_cycle = 0.0
        else:
            own_purchase_count = 0
            own_days_since = 0
            own_cycle = 0.0
        cand = Candidate(
            item_code=item,
            recommended_qty=qty,
            priority_score=round(pscore, 2),
            source="peer",
            van_qty=van_qty,
            avg_qty=median_qty,
            days_since=own_days_since,
            cycle_days=own_cycle,
            frequency_pct=round(score_val * 100, 1),
            pattern_quality=0.5,
            purchase_count=own_purchase_count,
            trend_factor=1.0,
        )
        out.append(_finalize(cand, expl))
    return out


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    """Weighted median: smallest value where cumulative weight >= half total."""
    if values.size == 0:
        return 0.0
    order = np.argsort(values)
    v = values[order]
    w = weights[order]
    total = float(w.sum())
    if total <= 0:
        return float(np.median(v))
    cum = np.cumsum(w)
    cutoff = total / 2.0
    idx = int(np.searchsorted(cum, cutoff))
    idx = min(idx, v.size - 1)
    return float(v[idx])


# gen_basket_complement: items that co-purchase with history picks.

def gen_basket_complement(
    history_picks: list[Candidate],
    van_items: dict[str, int],
    already_selected: set[str],
    *,
    co_occurrence: dict[str, dict[str, float]],   # anchor -> {item: P(item|anchor)}
    co_median_qty: dict[str, dict[str, float]],   # anchor -> {item: median qty when co-bought}
    calibration: RouteCalibration,
    clamps: SafetyClamps,
    item_names: dict[str, str],
) -> list[Candidate]:
    if not history_picks:
        return []
    min_conf = calibration.basket_min_confidence

    # Best (anchor, confidence) per candidate item
    best: dict[str, tuple[str, float]] = {}
    for anchor in history_picks:
        lookup = co_occurrence.get(anchor.item_code, {})
        for item, conf in lookup.items():
            if conf < min_conf:
                continue
            if item in already_selected or item == anchor.item_code:
                continue
            if int(van_items.get(item, 0)) <= 0:
                continue
            prev = best.get(item)
            if prev is None or conf > prev[1]:
                best[item] = (anchor.item_code, conf)

    # Keep top-K by confidence
    ranked = sorted(best.items(), key=lambda kv: -kv[1][1])[: clamps.basket_complement_top_k]

    out: list[Candidate] = []
    for item, (anchor_code, conf) in ranked:
        median_qty = float(
            co_median_qty.get(anchor_code, {}).get(item, 1.0)
        )
        median_qty = max(1.0, median_qty)
        van_qty = int(van_items.get(item, 0))
        if van_qty <= 0:
            continue
        qty = min(van_qty, int(np.ceil(median_qty)))
        if qty <= 0:
            continue
        anchor_label = item_names.get(anchor_code, anchor_code)

        expl = Explanation()
        expl.add_item_signal(Signal(
            kind=KIND_BASKET_COMPLEMENT,
            detail=detail_basket_complement(anchor_label, conf),
            weight=conf,
            evidence={"anchor": anchor_code, "confidence": round(conf, 3)},
        ))
        expl.add_quantity_signal(Signal(
            kind="qty_derivation",
            detail=detail_qty_basket(median_qty),
            weight=1.0,
            evidence={"median_qty": round(median_qty, 2), "van_cap": van_qty},
        ))
        score = calibration.tier_cuts["consider"] + conf * (
            calibration.tier_cuts["should_stock"] - calibration.tier_cuts["consider"]
        )
        cand = Candidate(
            item_code=str(item),
            recommended_qty=qty,
            priority_score=round(score, 2),
            source="basket",
            van_qty=van_qty,
            avg_qty=median_qty,
            days_since=0,
            cycle_days=0.0,
            frequency_pct=round(conf * 100, 1),
            pattern_quality=0.5,
            purchase_count=0,
            trend_factor=1.0,
        )
        out.append(_finalize(cand, expl))
    return out


# ===========================================================================
# gen_reactivation -- customer on journey but last buy > dormancy_days ago
# ===========================================================================

def gen_reactivation(
    customer: str,
    cust_history: pd.DataFrame,
    top_van_items: list[tuple[str, int]],
    target_dt: pd.Timestamp,
    *,
    calibration: RouteCalibration,
    clamps: SafetyClamps,
) -> list[Candidate]:
    if cust_history is None or cust_history.empty:
        return []
    last = cust_history["TrxDate"].max()
    if pd.isna(last):
        return []
    days_since_any = (target_dt - last).days

    # Re-awakened customers (recent buy inside dormancy window) flow through
    # history lane instead; this early exit makes that explicit.
    if days_since_any <= calibration.dormancy_days:
        return []

    out: list[Candidate] = []
    seed_qty = clamps.seed_qty
    for item, van_qty in top_van_items[: clamps.seed_top_k]:
        if van_qty <= 0:
            continue
        qty = max(1, min(seed_qty, int(van_qty)))
        expl = Explanation()
        expl.add_item_signal(Signal(
            kind=KIND_REACTIVATION,
            detail=detail_reactivation(days_since_any),
            weight=1.0,
            evidence={"days_since_any_purchase": days_since_any},
        ))
        expl.add_quantity_signal(Signal(
            kind="qty_derivation",
            detail=detail_qty_seed(qty),
            weight=1.0,
            evidence={"seed_qty": qty, "van_cap": int(van_qty)},
        ))
        score = calibration.tier_cuts["consider"]
        cand = Candidate(
            item_code=str(item),
            recommended_qty=qty,
            priority_score=round(score, 2),
            source="reactivation",
            van_qty=int(van_qty),
            avg_qty=float(qty),
            days_since=days_since_any,
            cycle_days=0.0,
            frequency_pct=0.0,
            pattern_quality=0.4,
            purchase_count=int(len(cust_history)),
            trend_factor=1.0,
            churn_probability=0.5,
        )
        out.append(_finalize(cand, expl))
    return out


# gen_seed: zero-history customers (first visit).

def gen_seed(
    customer: str,
    top_van_items: list[tuple[str, int]],
    *,
    calibration: RouteCalibration,
    clamps: SafetyClamps,
) -> list[Candidate]:
    out: list[Candidate] = []
    seed_qty = clamps.seed_qty
    for item, van_qty in top_van_items[: clamps.seed_top_k]:
        if van_qty <= 0:
            continue
        qty = max(1, min(seed_qty, int(van_qty)))
        expl = Explanation()
        expl.add_item_signal(Signal(
            kind=KIND_FIRST_VISIT,
            detail=detail_first_visit(),
            weight=1.0,
            evidence={},
        ))
        expl.add_quantity_signal(Signal(
            kind="qty_derivation",
            detail=detail_qty_seed(qty),
            weight=1.0,
            evidence={"seed_qty": qty, "van_cap": int(van_qty)},
        ))
        score = calibration.tier_cuts["consider"]
        cand = Candidate(
            item_code=str(item),
            recommended_qty=qty,
            priority_score=round(score, 2),
            source="seed",
            van_qty=int(van_qty),
            avg_qty=float(qty),
            days_since=0,
            cycle_days=0.0,
            frequency_pct=0.0,
            pattern_quality=0.5,
            purchase_count=0,
            trend_factor=1.0,
            churn_probability=0.5,
        )
        out.append(_finalize(cand, expl))
    return out


# merge_and_rank.

def merge_and_rank(candidates: list[Candidate]) -> list[Candidate]:
    """De-dupe by item_code (keep highest priority; merge signal lists;
    promote real metric values from loser when winner has 0 placeholders)
    then sort by priority desc."""
    by_item: dict[str, Candidate] = {}
    for cand in candidates:
        prev = by_item.get(cand.item_code)
        if prev is None or cand.priority_score > prev.priority_score:
            if prev is not None:
                cand = _merge(cand, prev)
            by_item[cand.item_code] = cand
        else:
            by_item[cand.item_code] = _merge(prev, cand)
    return sorted(by_item.values(), key=lambda c: -c.priority_score)


# History-derived metric fields that non-history generators emit as 0
# placeholders; merge promotes the loser's value when it's informative.
_HISTORY_METRIC_FIELDS: tuple[str, ...] = (
    "days_since", "cycle_days", "frequency_pct", "pattern_quality",
    "purchase_count", "trend_factor", "avg_qty", "churn_probability",
)


def _merge(winner: Candidate, loser: Candidate) -> Candidate:
    """Merge loser into winner: signals (de-duped by kind) + any real
    history-derived metric the winner lacks. Winner's score/source/qty
    are load-bearing and preserved."""
    # Signals
    seen = {s["kind"] for s in winner.signals}
    extra = [s for s in loser.signals if s["kind"] not in seen]
    if extra:
        winner.signals = winner.signals + extra
    # Metric backfill: take loser's value when winner has the field at its
    # default (trend_factor==1.0 treated as neutral).
    for name in _HISTORY_METRIC_FIELDS:
        wv = getattr(winner, name)
        lv = getattr(loser, name)
        winner_default = (wv == 0 or wv == 0.0) or (name == "trend_factor" and wv == 1.0)
        loser_informative = (lv != 0 and lv != 0.0) and not (name == "trend_factor" and lv == 1.0)
        if winner_default and loser_informative:
            setattr(winner, name, lv)
    return winner
