"""
Candidate generators -- each produces ``Candidate`` rows for one scoring lane.

Ordered evaluation in the engine:

    gen_history            -- the customer's own purchase patterns
    gen_peer_cross_sell    -- items popular on the route this customer doesn't buy
    gen_basket_complement  -- items that co-purchase with the history picks
    gen_reactivation       -- long-dormant customers on today's journey
    gen_seed               -- zero-history customers (first visit)

Each generator is a pure function: (inputs) -> list[Candidate]. No I/O,
no mutation of its inputs. The engine de-dupes and ranks across lanes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from recommended_order.config.constants import SafetyClamps, UniversalFilters
from recommended_order.core.calibration import RouteCalibration
from recommended_order.core.cycle import CycleCalculator
from recommended_order.core.explain import (
    Explanation,
    KIND_BASKET_COMPLEMENT,
    KIND_FIRST_VISIT,
    KIND_LOOKALIKE_PEER,
    KIND_REACTIVATION,
    KIND_TRENDING_DOWN,
    KIND_TRENDING_UP,
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


# ===========================================================================
# gen_history -- the customer's own purchase patterns
# ===========================================================================

def gen_history(
    customer: str,
    cust_history: pd.DataFrame,
    item_dict: Dict[str, pd.DataFrame],
    van_items: Dict[str, int],
    target_dt: pd.Timestamp,
    *,
    calibration: RouteCalibration,
    universal: UniversalFilters,
    clamps: SafetyClamps,
    cycle_calc: CycleCalculator,
    priority_calc: PriorityCalculator,
    quantity_calc: QuantityCalculator,
    trend_calc: TrendCalculator,
) -> List[Candidate]:
    out: List[Candidate] = []
    total_visits = int(cust_history["TrxDate"].nunique()) if not cust_history.empty else 0
    if total_visits == 0:
        return out

    for item, van_qty in van_items.items():
        if van_qty <= 0 or item not in item_dict:
            continue
        hist = item_dict[item]
        if hist.empty:
            continue

        last_purchase = pd.to_datetime(hist["TrxDate"]).max()
        days_since = (target_dt - last_purchase).days
        if days_since <= universal.min_days_since_purchase:
            continue

        item_visits = int(hist["TrxDate"].nunique())
        frequency = item_visits / total_visits if total_visits > 0 else 0.0
        if frequency < calibration.frequency_floor:
            continue

        purchase_count = len(hist)
        cycle_info = cycle_calc.calculate(hist, target_dt)
        cycle_days = max(1, cycle_info.cycle_days)
        completion = days_since / cycle_days
        if completion < calibration.completion_gate:
            continue

        expl = Explanation()
        pri = priority_calc.calculate(
            hist, cust_history, target_dt,
            cycle_days=cycle_days, days_since=days_since,
            item_frequency=frequency, calibration=calibration,
            explanation=expl,
        )

        trend = trend_calc.calculate(hist, target_dt)
        if trend.trend_type in ("ACCELERATING_FAST", "ACCELERATING"):
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
        elif trend.trend_type in ("DECLINING", "DECLINING_FAST"):
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


# ===========================================================================
# gen_peer_cross_sell -- lookalike-customer cross-sell (Sprint 2)
# ===========================================================================
#
# Replaces the Sprint-1 route-popularity heuristic. New algorithm:
#
#   1. ``lookalike_ctx`` holds a recency-weighted customer x item matrix and
#      the precomputed pairwise cosine similarities (built once per route by
#      ``engine._lookalike_context`` using ``calibration.recency_half_life_days``).
#   2. For the target customer we pick top-K most-similar peers where
#      K = ``calibration.peer_lookalike_k``.
#   3. For every van item the target has not bought, score =
#      sum(sim_p * weight[p, item]) / sum(sim_p). Normalisation uses ONLY
#      the similarities of peers who actually bought the item -- a peer
#      who didn't buy the item shouldn't pull the denominator up.
#   4. Emit candidates whose score clears the data-driven floor
#      ``max(calibration.peer_lookalike_floor, P75(observed_scores))``.
#   5. Quantity = similarity-weighted median across the K peers who bought
#      the item (never the route median -- fixes the Sprint-1 bug).
#   6. Top-``calibration.peer_max_per_customer`` per target.

def gen_peer_cross_sell(
    customer: str,
    item_dict: Dict[str, pd.DataFrame],
    van_items: Dict[str, int],
    *,
    lookalike_ctx: Dict[str, Any],
    calibration: RouteCalibration,
    clamps: SafetyClamps,
) -> List[Candidate]:
    cust_idx_map: Dict[str, int] = lookalike_ctx.get("cust_idx", {})
    item_idx_map: Dict[str, int] = lookalike_ctx.get("item_idx", {})
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

    # Score items: per-item similarity-weighted mean of recency weights,
    # normalised only across peers who bought the item.
    #
    # Edge case (Sprint-3, Theme C.3): brand-new SKUs.
    # A van item that nobody on the route has ever bought (e.g. a product
    # launched yesterday) will have ``peer_weights[:, j] == 0`` for every
    # peer. ``denom`` collapses to 0 for that column, ``np.where`` yields
    # score=0, and the item is cleanly filtered out by the ``>= floor`` gate
    # below. No exception, no special casing.
    bought_mask = peer_weights > 0                      # (K, n_items)
    sim_col = top_sims[:, None]                         # (K, 1)
    numer = (sim_col * peer_weights).sum(axis=0)        # (n_items,)
    denom = (sim_col * bought_mask).sum(axis=0)         # (n_items,)
    with np.errstate(invalid="ignore", divide="ignore"):
        scores = np.where(denom > 0, numer / denom, 0.0)

    # We intentionally do NOT drop items the target has in their own history:
    # history-lane picks will outrank peer for those, but a high-conviction peer
    # signal still flows through merge_and_rank as a corroborating Signal, and
    # items with only faint or stale history get a useful second opinion here.

    # Restrict to van items that exist in the matrix
    van_mask = np.zeros_like(scores, dtype=bool)
    van_col_to_code: Dict[int, str] = {}
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

    out: List[Candidate] = []
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
        qty = min(van_qty, max(1, int(round(median_qty))))
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
        cand = Candidate(
            item_code=item,
            recommended_qty=qty,
            priority_score=round(pscore, 2),
            source="peer",
            van_qty=van_qty,
            avg_qty=median_qty,
            days_since=0,
            cycle_days=0.0,
            frequency_pct=round(score_val * 100, 1),
            pattern_quality=0.5,
            purchase_count=0,
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


# ===========================================================================
# gen_basket_complement -- items that co-purchase with the history picks
# ===========================================================================

def gen_basket_complement(
    history_picks: List[Candidate],
    van_items: Dict[str, int],
    already_selected: set[str],
    *,
    co_occurrence: Dict[str, Dict[str, float]],   # anchor -> {item: P(item|anchor)}
    co_median_qty: Dict[str, Dict[str, float]],   # anchor -> {item: median qty when co-bought}
    calibration: RouteCalibration,
    clamps: SafetyClamps,
    item_names: Dict[str, str],
) -> List[Candidate]:
    if not history_picks:
        return []
    min_conf = calibration.basket_min_confidence

    # Best (anchor, confidence) per candidate item
    best: Dict[str, Tuple[str, float]] = {}
    for anchor in history_picks:
        lookup = co_occurrence.get(anchor.item_code, {})
        qty_lookup = co_median_qty.get(anchor.item_code, {})
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
        # Touch qty_lookup to keep the reference active; actually used below.
        del qty_lookup

    # Keep top-K by confidence
    ranked = sorted(best.items(), key=lambda kv: -kv[1][1])[: clamps.basket_complement_top_k]

    out: List[Candidate] = []
    for item, (anchor_code, conf) in ranked:
        median_qty = float(
            co_median_qty.get(anchor_code, {}).get(item, 1.0)
        )
        median_qty = max(1.0, median_qty)
        van_qty = int(van_items.get(item, 0))
        if van_qty <= 0:
            continue
        qty = min(van_qty, int(round(median_qty)))
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
    top_van_items: List[Tuple[str, int]],
    target_dt: pd.Timestamp,
    *,
    calibration: RouteCalibration,
    clamps: SafetyClamps,
) -> List[Candidate]:
    if cust_history is None or cust_history.empty:
        return []
    last = pd.to_datetime(cust_history["TrxDate"]).max()
    if pd.isna(last):
        return []
    days_since_any = (target_dt - last).days

    # Edge case (Sprint-3, Theme C.6): returning customer (was dormant, now active).
    # A customer who had a 100-day silence but bought again 3 days ago is NOT
    # dormant today -- their last-buy is well inside calibration.dormancy_days
    # so we simply don't emit reactivation recs. The engine's history lane
    # drives their plan using the items they used to buy, and the quantity
    # recency-weighting (fresh purchases dominate) naturally supplies the
    # "fresh-start qty signal" the product brief calls for. This early exit
    # makes that behaviour explicit.
    if days_since_any <= calibration.dormancy_days:
        return []

    out: List[Candidate] = []
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


# ===========================================================================
# gen_seed -- zero-history customers
# ===========================================================================

def gen_seed(
    customer: str,
    top_van_items: List[Tuple[str, int]],
    *,
    calibration: RouteCalibration,
    clamps: SafetyClamps,
) -> List[Candidate]:
    out: List[Candidate] = []
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


# ===========================================================================
# merge_and_rank
# ===========================================================================

def merge_and_rank(candidates: List[Candidate]) -> List[Candidate]:
    """De-dupe by item_code (keep highest priority, merge signal lists),
    then sort by priority desc."""
    by_item: Dict[str, Candidate] = {}
    for cand in candidates:
        prev = by_item.get(cand.item_code)
        if prev is None or cand.priority_score > prev.priority_score:
            if prev is not None:
                # Merge the loser's item_signals into the winner's signals list
                cand = _merge_signals(cand, prev)
            by_item[cand.item_code] = cand
        else:
            by_item[cand.item_code] = _merge_signals(prev, cand)
    return sorted(by_item.values(), key=lambda c: -c.priority_score)


def _merge_signals(winner: Candidate, loser: Candidate) -> Candidate:
    """Append the loser's signals (de-duped by kind) to the winner."""
    seen = {s["kind"] for s in winner.signals}
    extra = [s for s in loser.signals if s["kind"] not in seen]
    if not extra:
        return winner
    winner.signals = winner.signals + extra
    return winner
