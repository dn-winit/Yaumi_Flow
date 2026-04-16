"""
Business constants for the recommendation engine.

Sprint 1 overhaul: every threshold that used to be a guess (0.75 completion
gate, 75/55/35/15 tier cuts, 50-unit qty benchmark, 90-day dormancy, etc.)
now lives in ``RouteCalibration`` -- derived per route from observed data.

What remains here is strictly:
    * safety clamps (hard floors/ceilings on calibration outputs)
    * tiny, non-business constants (cache TTL, storage settings)
    * the top-level container imported by DI

No hardcoded business thresholds. No dead code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


# ---------------------------------------------------------------------------
# Safety clamps -- the ONLY numeric "magic" allowed in business code
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SafetyClamps:
    """Hard bounds that calibration outputs are clamped to.

    These are not business thresholds -- they are guardrails preventing
    pathological calibration results when a route has too little data or
    a weird distribution (e.g. a route with one customer skewing P25).
    """

    # Item-frequency floor (share of visits that include the item)
    freq_floor_min: float = 0.02
    freq_floor_max: float = 0.50

    # Dormancy (how long since last visit before we flag a customer inactive)
    dormancy_min_days: int = 30
    dormancy_max_days: int = 180

    # Quantity benchmark (the "100% in log-scale" reference)
    qty_benchmark_min: float = 5.0
    qty_benchmark_max: float = 500.0

    # Completion gate -- lowest cycle-completion we will consider "due"
    completion_gate_floor: float = 0.30
    completion_gate_cap: float = 0.60

    # Basket-complement minimum confidence (P60 of observed confidences, clamped)
    basket_min_confidence_min: float = 0.30
    basket_min_confidence_max: float = 0.70

    # Sparse-route relief: if a route's active-customer count is below this
    # fraction of the corpus median, frequency_floor is lowered proportionally.
    sparse_route_customer_ratio: float = 0.75
    # Maximum relief applied to frequency_floor on the sparsest routes.
    sparse_route_freq_floor_relief: float = 0.5

    # Lookalike-peer cross-sell (Sprint-2)
    peer_lookalike_k_min: int = 5
    peer_lookalike_k_max: int = 20
    peer_lookalike_floor_min: float = 0.05
    peer_lookalike_floor_max: float = 0.50
    peer_max_per_customer_min: int = 2
    peer_max_per_customer_max: int = 8
    # Divisor used in `qty_benchmark // factor` to derive peer_max_per_customer.
    peer_max_per_customer_qty_divisor: float = 10.0

    # Recency half-life (days) for exp-decay qty/cycle weighting
    recency_half_life_min: float = 14.0
    recency_half_life_max: float = 180.0

    # Tier cut bounds (priority-score 0-100 scale)
    tier_must_stock_min: float = 70.0
    tier_must_stock_max: float = 95.0
    tier_should_stock_min: float = 50.0
    tier_should_stock_max: float = 80.0
    tier_consider_min: float = 30.0
    tier_consider_max: float = 60.0
    tier_monitor_min: float = 10.0
    tier_monitor_max: float = 40.0

    # Quantity sizing -- "perfect zone" center (imported from sales_supervision)
    qty_center_lo: float = 0.85  # 0.85 x expected actual
    qty_center_hi: float = 1.10  # 1.10 x expected actual

    # Scoring lane caps (how many rows each generator may emit per customer)
    basket_complement_top_k: int = 3
    seed_top_k: int = 5

    # Reactivation cutoff: a customer's own dormancy threshold scaling
    reactivation_lapsed_days: int = 60

    # New-customer seed qty (low-risk starter)
    seed_qty: int = 5

    # ---- Sprint-3: calibration window (trailing days of history used) ----
    # The user thinks of this as "last N days of behaviour". Corpus-wide
    # fallback kicks in when the window has too little signal.
    calibration_window_days: int = 90
    calibration_window_min_days: int = 60
    calibration_window_max_days: int = 180
    calibration_fallback_min_days: int = 14       # need >= this many distinct days
    calibration_fallback_min_customers: int = 5   # need >= this many customers

    # Anti-overfit sanity clamp: a per-route calibration field that exceeds
    # this many stdevs from the corpus distribution gets clamped to the
    # percentile below.
    sanity_clamp_zscore: float = 3.0
    sanity_clamp_percentile: float = 95.0

    # ---- Sprint-3: feedback loop ----
    feedback_enabled: bool = True
    feedback_window_days: int = 30
    feedback_window_min_days: int = 14
    feedback_window_max_days: int = 180
    feedback_ema_alpha: float = 0.3               # smoothing for per-source multipliers
    feedback_multiplier_min: float = 0.5
    feedback_multiplier_max: float = 1.5
    # ---- Sprint-4: feedback shrinkage / attribution ----
    # Empirical-Bayes prior strength is derived per-run as
    #   k = max(1, corpus_median_n / feedback_prior_strength_divisor).
    # A route needs roughly k attributed samples to overcome the corpus prior.
    feedback_prior_strength_divisor: float = 4.0
    # Sessions whose per-route reject-rate exceeds this many stdevs from the
    # route's session-level distribution are treated as adversarial and
    # excluded from attribution (pure data-driven; no hardcoded supervisors).
    feedback_adversarial_zscore: float = 3.0
    # Corpus-wide hit-rate floor: if a source's corpus hit rate falls below
    # this we log a warning (the source itself is weak; fix is out-of-scope
    # for the multiplier which is relative).
    feedback_bad_source_floor: float = 0.05
    # Persisted multipliers path (relative to shared_data_dir). Small (KB) --
    # stores only the multipliers + sample count, never session data.
    feedback_multipliers_filename: str = "feedback_multipliers.json"

    # ---- Sprint-3: cache safety (LRU + TTL) ----
    cache_ttl_seconds: int = 3600                 # 1h
    cache_max_entries: int = 50

    # ---- Sprint-3: outlier winsorisation for qty averaging ----
    outlier_winsor_percentile: float = 95.0

    # ---- Sprint-3: peer generator degeneracy guard ----
    peer_min_active_customers: int = 3

    # ---- Sprint-3: reactivation refinement ----
    # If the customer was dormant but recently bought (within this many days),
    # they're "re-awakening" -- route them back through history lane, not seed.
    reactivation_recent_buy_days: int = 14

    # ---- Sprint-3: generation metrics sink ----
    generation_metrics_max_bytes: int = 10 * 1024 * 1024   # 10 MB rotation


# ---------------------------------------------------------------------------
# Storage / infra settings (not business thresholds)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StorageSettings:
    """Database storage parameters."""
    batch_size: int = 1000
    max_save_attempts: int = 3
    generated_by: str = "SYSTEM_CRON"


# Analytics caches (adoption, planning) -- 5 min keeps drawer re-opens instant.
ANALYTICS_CACHE_TTL_SECONDS = 300


# ---------------------------------------------------------------------------
# Universal pre-calibration filters (kept: safety-level, not business thresholds)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UniversalFilters:
    """Pre-calibration filters that apply regardless of route calibration."""
    # Never recommend something a customer bought yesterday.
    min_days_since_purchase: int = 1
    # "Regular" path needs at least this many purchases of an item.
    min_purchase_count_standard: int = 3


# ---------------------------------------------------------------------------
# Top-level container (kept for DI compatibility)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RecommendationConstants:
    """Top-level container. All *business* thresholds have moved to
    ``RouteCalibration`` in ``core/calibration.py``; this object only
    carries safety clamps and infra settings."""
    clamps: SafetyClamps = field(default_factory=SafetyClamps)
    filters: UniversalFilters = field(default_factory=UniversalFilters)
    storage: StorageSettings = field(default_factory=StorageSettings)
