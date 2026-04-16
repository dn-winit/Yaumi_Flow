"""
Recommendation engine -- candidate-generator architecture.

Sprint-1 redesign:
    1. Calibrate thresholds from route data (`core/calibration.py`).
    2. Per-customer: run generators in order (history, peer cross-sell,
       basket complement, reactivation, seed). Each generator returns a
       list of ``Candidate`` objects with its own scoring lane.
    3. Merge + rank across lanes.
    4. Apply van-load constraints (unchanged).

Sprint-3 robustness:
    * **Circuit breaker** per generator -- one throwing generator never fails
      a route; we log and continue with the rest.
    * **Lookalike cache** (LRU + TTL) -- per-route matrix/similarity blocks
      are expensive; repeated generations for the same route reuse them.
      Previously unbounded.
    * **Per-generator metrics** emitted per-route: single-line key=value log
      + CSV sink + in-memory snapshot for the ``/metrics/last-generation``
      endpoint.
    * **Thread-safe cache mutations** via a dedicated lock.
    * **Peer degeneracy guard** -- micro-routes (< ``peer_min_active_customers``
      customers) skip peer generator with a route-level log line.
    * **Feedback multipliers** are applied to per-source candidate priority
      scores when ``SafetyClamps.feedback_enabled`` is True.

Explainability is carried end-to-end on each Candidate via ``Signals``
(list[dict]), ``WhyItem``, ``WhyQuantity`` and ``Confidence``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd

from recommended_order.config.constants import RecommendationConstants
from recommended_order.config.settings import get_settings
from recommended_order.core.calibration import (
    RouteCalibration,
    calibrate,
    cache_size as calibration_cache_size,
    classify_tier,
)
from recommended_order.core.constraints import apply_van_load_constraints
from recommended_order.core.cycle import CycleCalculator
from recommended_order.core.feedback import apply_adjustments_to_candidates
from recommended_order.core.generators import (
    gen_basket_complement,
    gen_history,
    gen_peer_cross_sell,
    gen_reactivation,
    gen_seed,
    merge_and_rank,
)
from recommended_order.core.metrics import (
    get_last_generation_tracker,
    get_metrics_csv_sink,
    log_gen_metrics_line,
)
from recommended_order.core.priority import PriorityCalculator
from recommended_order.core.quantity import QuantityCalculator
from recommended_order.core.trend import TrendCalculator
from recommended_order.models.recommendation import Candidate, Recommendation

logger = logging.getLogger(__name__)


class RecommendationEngine:
    """Orchestrates calibration + generators + constraints."""

    def __init__(self, constants: Optional[RecommendationConstants] = None) -> None:
        self._c = constants or RecommendationConstants()
        self._trend = TrendCalculator()
        self._priority = PriorityCalculator()
        self._quantity = QuantityCalculator(self._c.clamps)
        self._corpus_median_customers: Optional[float] = None
        # Corpus-wide distributions of each calibration field, used as the
        # reference for ``_sanity_clamp`` in calibration.py. Set by the API
        # layer once per generation pass.
        self._corpus_field_values: Optional[Dict[str, List[float]]] = None
        # Per-source feedback multipliers keyed by route_code.
        self._feedback_adjustments: Dict[str, Dict[str, float]] = {}
        # Sprint-4: per-source confidence in the multiplier (same keying).
        self._feedback_confidence: Dict[str, Dict[str, float]] = {}
        # LRU + TTL cache for the per-route lookalike context (matrix +
        # similarities). Key = (route_code, csv_mtime, window_days).
        self._lookalike_cache: "OrderedDict[Tuple[str, float, int], Tuple[float, Dict[str, Any]]]" = OrderedDict()
        self._cache_lock = threading.Lock()

    def set_corpus_stats(
        self,
        *,
        median_active_customers: Optional[float],
        field_values: Optional[Dict[str, List[float]]] = None,
    ) -> None:
        """Inject corpus-level stats the engine can't compute per-route."""
        self._corpus_median_customers = median_active_customers
        self._corpus_field_values = field_values

    def set_feedback_adjustments(
        self,
        adjustments: Dict[str, Dict[str, float]],
        *,
        confidence: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> None:
        """Inject the latest per-route per-source feedback multipliers and
        (Sprint-4) the per-route per-source sample-size confidence."""
        self._feedback_adjustments = adjustments or {}
        self._feedback_confidence = confidence or {}

    def feedback_routes_active(self) -> int:
        """Count routes with at least one non-1.0 source multiplier."""
        n = 0
        for per_source in self._feedback_adjustments.values():
            if any(abs(v - 1.0) > 1e-6 for v in per_source.values()):
                n += 1
        return n

    def lookalike_cache_size(self) -> int:
        with self._cache_lock:
            return len(self._lookalike_cache)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def generate(
        self,
        customer_df: pd.DataFrame,
        journey_customers: List[str],
        van_items: Dict[str, int],
        item_names: Dict[str, str],
        customer_names: Dict[str, str],
        route_code: str,
        target_date: str,
        demand_df: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        t0 = time.time()
        target_dt = pd.to_datetime(target_date).normalize()

        # 1. Calibrate thresholds for this route (with window + corpus sanity clamp)
        calibration = calibrate(
            customer_df=customer_df,
            demand_df=demand_df if demand_df is not None else pd.DataFrame(),
            route_code=route_code,
            clamps=self._c.clamps,
            corpus_median_customers=self._corpus_median_customers,
            window_days=self._c.clamps.calibration_window_days,
            corpus_field_values=self._corpus_field_values,
            source_weight_adjustments=self._feedback_adjustments.get(route_code),
            feedback_confidence=self._feedback_confidence.get(route_code),
        )
        cycle_calc = CycleCalculator(calibration.recency_half_life_days)

        # 2. Route-level pre-compute (done ONCE per route)
        histories = self._precompute(customer_df, journey_customers)
        top_van_items = self._top_van_items(van_items, self._c.clamps.seed_top_k)

        # Edge case (Sprint-3, Theme C.4): micro-route / degenerate peer input.
        # Peer similarity on < N customers is numerical noise; we skip the peer
        # generator entirely for tiny routes and emit a route-level log line.
        active_customers = int(customer_df["CustomerCode"].nunique()) if not customer_df.empty else 0
        peer_enabled = active_customers >= self._c.clamps.peer_min_active_customers
        if not peer_enabled:
            logger.info(
                "peer_skip route=%s reason=micro_route active_customers=%d threshold=%d",
                route_code, active_customers, self._c.clamps.peer_min_active_customers,
            )
            lookalike_ctx: Dict[str, Any] = {}
        else:
            lookalike_ctx = self._get_or_build_lookalike(
                route_code, customer_df, target_dt, calibration,
            )

        co_occurrence, co_median = self._co_occurrence(customer_df, calibration)

        records: List[Dict[str, Any]] = []
        counts = {"journey": len(journey_customers), "active": 0, "reactivated": 0, "seeded": 0}

        # Per-generator tallies for metrics
        gen_stats: Dict[str, Dict[str, Any]] = {
            "history": {"candidates": 0, "kept": 0},
            "peer": {"candidates": 0, "kept": 0, "similarity_sum": 0.0, "similarity_n": 0},
            "basket": {"candidates": 0, "kept": 0},
            "reactivation": {"candidates": 0, "kept": 0},
            "seed": {"candidates": 0, "kept": 0},
        }

        adjustments = (
            self._feedback_adjustments.get(route_code, {})
            if self._c.clamps.feedback_enabled
            else {}
        )
        adjust_conf = (
            self._feedback_confidence.get(route_code, {})
            if self._c.clamps.feedback_enabled
            else {}
        )

        for customer in sorted(journey_customers):
            cust_data = histories.get(customer)

            # --- Unknown customer: zero history ---
            if cust_data is None:
                cands = self._safe_call(
                    "seed", route_code,
                    lambda: gen_seed(
                        customer, top_van_items,
                        calibration=calibration, clamps=self._c.clamps,
                    ),
                )
                gen_stats["seed"]["candidates"] += len(cands)
                if cands:
                    counts["seeded"] += 1
                kept = self._rows(cands, customer, target_dt, route_code, item_names, customer_names, calibration)
                gen_stats["seed"]["kept"] += len(kept)
                records.extend(kept)
                continue

            cust_history = cust_data["history"]
            item_dict = cust_data["items"]

            all_cands: List[Candidate] = []

            history_cands = self._safe_call(
                "history", route_code,
                lambda: gen_history(
                    customer, cust_history, item_dict, van_items, target_dt,
                    calibration=calibration,
                    universal=self._c.filters,
                    clamps=self._c.clamps,
                    cycle_calc=cycle_calc,
                    priority_calc=self._priority,
                    quantity_calc=self._quantity,
                    trend_calc=self._trend,
                ),
            )
            gen_stats["history"]["candidates"] += len(history_cands)
            all_cands.extend(history_cands)

            if peer_enabled:
                peer_cands = self._safe_call(
                    "peer", route_code,
                    lambda: gen_peer_cross_sell(
                        customer, item_dict, van_items,
                        lookalike_ctx=lookalike_ctx,
                        calibration=calibration,
                        clamps=self._c.clamps,
                    ),
                )
                gen_stats["peer"]["candidates"] += len(peer_cands)
                for pc in peer_cands:
                    ev = (pc.signals[0] if pc.signals else {}).get("evidence", {}) if pc.signals else {}
                    sim = ev.get("similarity_score") if isinstance(ev, dict) else None
                    if isinstance(sim, (int, float)):
                        gen_stats["peer"]["similarity_sum"] += float(sim)
                        gen_stats["peer"]["similarity_n"] += 1
                all_cands.extend(peer_cands)

            already = {c.item_code for c in all_cands}
            basket_cands = self._safe_call(
                "basket", route_code,
                lambda: gen_basket_complement(
                    history_cands, van_items, already,
                    co_occurrence=co_occurrence,
                    co_median_qty=co_median,
                    calibration=calibration,
                    clamps=self._c.clamps,
                    item_names=item_names,
                ),
            )
            gen_stats["basket"]["candidates"] += len(basket_cands)
            all_cands.extend(basket_cands)

            reacts = self._safe_call(
                "reactivation", route_code,
                lambda: gen_reactivation(
                    customer, cust_history, top_van_items, target_dt,
                    calibration=calibration, clamps=self._c.clamps,
                ),
            )
            gen_stats["reactivation"]["candidates"] += len(reacts)
            all_cands.extend(reacts)
            if reacts:
                counts["reactivated"] += 1
            else:
                counts["active"] += 1

            # Sprint-3/4: feedback multipliers applied to priority scores
            # *before* the dedupe-by-item step in merge_and_rank -- a
            # strong-source candidate wins over a weak-source one.
            if adjustments:
                apply_adjustments_to_candidates(
                    all_cands, adjustments, confidence=adjust_conf,
                )

            ranked = merge_and_rank(all_cands)
            kept_rows = self._rows(ranked, customer, target_dt, route_code, item_names, customer_names, calibration)
            # Attribute kept rows back to their originating source for metrics.
            for r in kept_rows:
                src = r.get("Source", "")
                if src in gen_stats:
                    gen_stats[src]["kept"] += 1
            records.extend(kept_rows)

        # Emit per-generator structured logs
        gen_metrics_payload: List[Dict[str, Any]] = []
        date_str = str(target_dt.date())
        now_iso = pd.Timestamp.utcnow().isoformat()
        total_kept = sum(s["kept"] for s in gen_stats.values())
        for gname, s in gen_stats.items():
            extras: Dict[str, Any] = {}
            if gname == "peer" and s.get("similarity_n", 0) > 0:
                extras["similarity_avg"] = round(
                    s["similarity_sum"] / max(1, s["similarity_n"]), 4
                )
            log_gen_metrics_line(route_code, gname, s["candidates"], s["kept"], extras)
            source_pct = round(100.0 * s["kept"] / total_kept, 2) if total_kept else 0.0
            gen_metrics_payload.append({
                "timestamp": now_iso,
                "date": date_str,
                "route": route_code,
                "gen": gname,
                "candidates": s["candidates"],
                "kept": s["kept"],
                "source_pct": source_pct,
                "similarity_avg": extras.get("similarity_avg", ""),
                "calibration_fallback": calibration.derived_from.get("window", {}).get("fallback", False),
            })

        # Persist per-generation metrics (CSV + in-memory tracker)
        duration = time.time() - t0
        try:
            sink = get_metrics_csv_sink(
                get_settings().shared_data_dir, self._c.clamps,
            )
            sink.append(gen_metrics_payload)
        except Exception as exc:  # pragma: no cover -- non-critical
            logger.warning("Failed to append to generation_metrics.csv: %s", exc)

        get_last_generation_tracker().record(
            route_code=route_code,
            target_date=date_str,
            gen_metrics=gen_metrics_payload,
            calibration_summary=self._calibration_summary(calibration),
            duration_seconds=duration,
        )

        logger.info(
            "route=%s date=%s journey=%d active=%d reactivated=%d seeded=%d rows=%d "
            "calib[freq_floor=%.3f dormancy=%dd qty_bench=%.1f completion_gate=%.2f fallback=%s] "
            "duration=%.2fs",
            route_code, target_dt.date(),
            counts["journey"], counts["active"], counts["reactivated"], counts["seeded"],
            len(records),
            calibration.frequency_floor, calibration.dormancy_days,
            calibration.qty_benchmark, calibration.completion_gate,
            calibration.derived_from.get("window", {}).get("fallback", False),
            duration,
        )

        df = pd.DataFrame(records)
        if df.empty:
            return df
        return apply_van_load_constraints(df)

    # ------------------------------------------------------------------
    # Circuit breaker + small utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_call(
        gen_name: str, route_code: str, fn: Callable[[], List[Candidate]],
    ) -> List[Candidate]:
        """Circuit breaker: swallow generator exceptions + log, never fail route."""
        try:
            return fn() or []
        except Exception as exc:  # pragma: no cover -- defensive
            logger.error(
                "generator_failed route=%s gen=%s error=%r",
                route_code, gen_name, exc, exc_info=True,
            )
            return []

    @staticmethod
    def _calibration_summary(calib: RouteCalibration) -> Dict[str, Any]:
        """Slim, JSON-safe snapshot for the metrics endpoint."""
        window = calib.derived_from.get("window", {})
        return {
            "frequency_floor": round(calib.frequency_floor, 4),
            "dormancy_days": int(calib.dormancy_days),
            "qty_benchmark": round(calib.qty_benchmark, 2),
            "completion_gate": round(calib.completion_gate, 4),
            "basket_min_confidence": round(calib.basket_min_confidence, 4),
            "peer_lookalike_k": int(calib.peer_lookalike_k),
            "window": window,
            "source_weight_adjustments": calib.source_weight_adjustments or {},
            "feedback_confidence": calib.feedback_confidence or {},
        }

    # ------------------------------------------------------------------
    # Lookalike cache (LRU + TTL + lock)
    # ------------------------------------------------------------------

    def _get_or_build_lookalike(
        self,
        route_code: str,
        customer_df: pd.DataFrame,
        target_dt: pd.Timestamp,
        calibration: RouteCalibration,
    ) -> Dict[str, Any]:
        c = self._c.clamps
        # Key by (route, csv_mtime, window_days) -- same invalidation as calibration.
        from recommended_order.core.calibration import _csv_mtime as _mt
        key = (route_code, _mt(), c.calibration_window_days)
        now = time.time()
        with self._cache_lock:
            entry = self._lookalike_cache.get(key)
            if entry is not None and now - entry[0] <= c.cache_ttl_seconds:
                self._lookalike_cache.move_to_end(key)
                return entry[1]
            # Stale or missing -- drop if present, rebuild outside the lock to
            # avoid blocking concurrent requests for other routes.
            if entry is not None:
                self._lookalike_cache.pop(key, None)
        ctx = self._lookalike_context(customer_df, target_dt, calibration)
        with self._cache_lock:
            self._lookalike_cache[key] = (now, ctx)
            self._lookalike_cache.move_to_end(key)
            while len(self._lookalike_cache) > c.cache_max_entries:
                self._lookalike_cache.popitem(last=False)
        return ctx

    # ------------------------------------------------------------------
    # Candidate -> row serialisation
    # ------------------------------------------------------------------

    def _rows(
        self,
        candidates: List[Candidate],
        customer: str,
        target_dt: pd.Timestamp,
        route_code: str,
        item_names: Dict[str, str],
        customer_names: Dict[str, str],
        calibration: RouteCalibration,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for cand in candidates:
            tier = classify_tier(cand.priority_score, calibration.tier_cuts)
            if tier == "EXCLUDE":
                continue
            rows.append(Recommendation(
                trx_date=str(target_dt.date()),
                route_code=str(route_code),
                customer_code=str(customer),
                customer_name=customer_names.get(customer, ""),
                item_code=str(cand.item_code),
                item_name=item_names.get(cand.item_code, ""),
                recommended_quantity=int(cand.recommended_qty),
                tier=tier,
                van_load=int(cand.van_qty),
                priority_score=float(cand.priority_score),
                avg_quantity_per_visit=int(round(cand.avg_qty)),
                days_since_last_purchase=int(cand.days_since),
                purchase_cycle_days=float(cand.cycle_days),
                frequency_percent=float(cand.frequency_pct),
                churn_probability=float(cand.churn_probability),
                pattern_quality=float(cand.pattern_quality),
                purchase_count=int(cand.purchase_count),
                trend_factor=float(cand.trend_factor),
                signals=cand.signals,
                why_item=cand.why_item,
                why_quantity=cand.why_quantity,
                confidence=float(cand.confidence),
                candidate_source=cand.source,
            ).to_dict())
        return rows

    # ------------------------------------------------------------------
    # Route-level pre-compute
    # ------------------------------------------------------------------

    @staticmethod
    def _top_van_items(van_items: Dict[str, int], k: int) -> List[Tuple[str, int]]:
        if k <= 0 or not van_items:
            return []
        # Edge case (Sprint-3, Theme C.2): stockouts.
        # An item with van_qty == 0 or negative must not reach the engine.
        # ``dm.get_van_items`` already filters non-positive quantities; this
        # second guard is defence-in-depth so a direct caller can't slip one
        # through.
        ranked = sorted(van_items.items(), key=lambda kv: (-int(kv[1] or 0), kv[0]))
        return [(code, int(qty)) for code, qty in ranked[:k] if int(qty or 0) > 0]

    @staticmethod
    def _precompute(
        customer_df: pd.DataFrame,
        journey_customers: List[str],
    ) -> Dict[str, Dict]:
        journey_set = set(journey_customers)
        relevant = customer_df[customer_df["CustomerCode"].isin(journey_set)]
        if relevant.empty:
            return {}
        out: Dict[str, Dict] = {}
        for cust_code, cust_group in relevant.groupby("CustomerCode", sort=False):
            out[str(cust_code)] = {
                "history": cust_group,
                "items": {
                    str(ic): grp for ic, grp in cust_group.groupby("ItemCode", sort=False)
                },
            }
        return out

    @staticmethod
    def _lookalike_context(
        customer_df: pd.DataFrame,
        target_dt: pd.Timestamp,
        calibration: RouteCalibration,
    ) -> Dict[str, Any]:
        """Build the customer-item recency-weighted matrix + cosine similarities."""
        empty = {
            "customers": [],
            "items": [],
            "cust_idx": {},
            "item_idx": {},
            "matrix": np.zeros((0, 0), dtype=float),
            "similarity": np.zeros((0, 0), dtype=float),
            "qty_matrix": np.zeros((0, 0), dtype=float),
        }
        if customer_df.empty:
            return empty

        half_life = float(calibration.recency_half_life_days)
        df = customer_df.copy()
        df["TrxDate"] = pd.to_datetime(df["TrxDate"])
        df["age_days"] = (target_dt - df["TrxDate"]).dt.days.clip(lower=0)
        df["weight"] = np.exp(-df["age_days"].astype(float) / max(1.0, half_life))

        weighted = (
            df.groupby(["CustomerCode", "ItemCode"])["weight"].sum().reset_index()
        )
        qty_src = "TotalQuantity" if "TotalQuantity" in df.columns else None
        if qty_src is not None:
            per_mean = df.groupby(["CustomerCode", "ItemCode"])[qty_src].mean().reset_index()
        else:
            per_mean = weighted.assign(TotalQuantity=1.0)
            qty_src = "TotalQuantity"

        customers = sorted(weighted["CustomerCode"].astype(str).unique().tolist())
        items = sorted(weighted["ItemCode"].astype(str).unique().tolist())
        if not customers or not items:
            return empty
        cust_idx = {c: i for i, c in enumerate(customers)}
        item_idx = {it: j for j, it in enumerate(items)}

        matrix = np.zeros((len(customers), len(items)), dtype=float)
        for cust, item, w in weighted.itertuples(index=False):
            matrix[cust_idx[str(cust)], item_idx[str(item)]] = float(w)

        qty_matrix = np.zeros_like(matrix)
        for cust, item, q in per_mean[["CustomerCode", "ItemCode", qty_src]].itertuples(index=False):
            qty_matrix[cust_idx[str(cust)], item_idx[str(item)]] = float(q)

        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        normed = matrix / norms
        similarity = normed @ normed.T
        np.fill_diagonal(similarity, 0.0)

        return {
            "customers": customers,
            "items": items,
            "cust_idx": cust_idx,
            "item_idx": item_idx,
            "matrix": matrix,
            "similarity": similarity,
            "qty_matrix": qty_matrix,
        }

    @staticmethod
    def _co_occurrence(
        customer_df: pd.DataFrame, calibration: RouteCalibration,
    ) -> Tuple[Dict[str, Dict[str, float]], Dict[str, Dict[str, float]]]:
        """Build item -> item co-occurrence confidence and median co-qty."""
        if customer_df.empty:
            return {}, {}
        cust_items: Dict[str, set] = {}
        cust_qty: Dict[Tuple[str, str], float] = {}
        grouped = customer_df.groupby(["CustomerCode", "ItemCode"])["TotalQuantity"].mean()
        for (cust, item), q in grouped.items():
            cust_items.setdefault(str(cust), set()).add(str(item))
            cust_qty[(str(cust), str(item))] = float(q)

        item_customers: Dict[str, set] = {}
        for cust, items in cust_items.items():
            for it in items:
                item_customers.setdefault(it, set()).add(cust)

        confidence: Dict[str, Dict[str, float]] = {}
        co_qty: Dict[str, Dict[str, float]] = {}
        min_conf = calibration.basket_min_confidence

        for a, a_customers in item_customers.items():
            if len(a_customers) < 3:
                continue
            conf_a: Dict[str, float] = {}
            qty_a: Dict[str, float] = {}
            for b, b_customers in item_customers.items():
                if a == b:
                    continue
                both = a_customers & b_customers
                if len(both) < 2:
                    continue
                p = len(both) / len(a_customers)
                if p < min_conf:
                    continue
                conf_a[b] = p
                qtys = [cust_qty[(c, b)] for c in both if (c, b) in cust_qty]
                if qtys:
                    qty_a[b] = float(np.median(qtys))
            if conf_a:
                confidence[a] = conf_a
                co_qty[a] = qty_a
        return confidence, co_qty
