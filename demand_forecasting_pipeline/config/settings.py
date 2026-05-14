"""
Application settings from environment variables.
Pipeline-specific ML params stay in config.yaml -- this handles server/API/paths.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

def _read_allow_origins() -> list[str]:
    """Read shared ``YF_ALLOW_ORIGINS`` (comma/semicolon string or JSON
    list). Falls back to local-dev origin. Bypasses pydantic-settings'
    JSON-only list parser so ops can set the env var as a plain string."""
    import json
    raw = os.getenv("YF_ALLOW_ORIGINS", "").strip()
    if not raw:
        return ["http://localhost:3000"]
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
    return [s.strip() for s in raw.replace(";", ",").split(",") if s.strip()]

_PIPELINE_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _PIPELINE_ROOT.parent


def _data_root() -> Path:
    """Resolve the unified on-disk data root. ``YF_DATA_ROOT`` env var
    moves every service's filesystem layout in lockstep; defaults to
    ``<project>/data`` for fresh checkouts."""
    raw = os.getenv("YF_DATA_ROOT", "").strip()
    return Path(raw).resolve() if raw else _PROJECT_ROOT / "data"


# Public constant: NVARCHAR widths matching the schema in
# scripts/create_tables.sql + scripts/migrations/0001_add_reconciliation_cols.sql.
# Imported by DbPusher so there is exactly one source of truth.
DEFAULT_STR_LIMITS: dict[str, int] = {
    "route_code": 50,
    "item_code": 50,
    "item_name": 255,
    "data_split": 20,
    "demand_class": 50,
    "model_used": 100,
}

class DbSettings(BaseSettings):
    """DB connection for pushing results to YaumiAIML."""

    model_config = {"env_prefix": "DF_DB_", "extra": "ignore"}

    host: str = Field(default="")
    port: int = Field(default=1433)
    database: str = Field(default="YaumiAIML")
    username: str = Field(default="")
    password: str = Field(default="")
    driver: str = Field(default="{ODBC Driver 17 for SQL Server}")
    connection_timeout: int = Field(default=120, ge=10)
    # Per-query (cursor) timeout for the bulk forecast push. Larger than
    # the connect handshake budget because executemany batches thousands
    # of rows in one round-trip.
    query_timeout: int = Field(default=300, ge=10)
    retry_attempts: int = Field(default=3, ge=1)
    retry_delay: int = Field(default=2, ge=1)
    # Rows per ``cursor.executemany`` batch. Large enough to amortise the
    # round-trip cost, small enough that a transient failure inside one
    # batch leaves a bounded amount of pending work to roll back.
    executemany_chunk_size: int = Field(default=1000, ge=1)
    # MERGE upsert isolation level. SERIALIZABLE prevents the classic
    # SQL Server MERGE phantom-row race when two writers target the
    # same key set; READ COMMITTED is the default if you want to trade
    # safety for shorter lock windows on a single-writer deployment.
    merge_isolation_level: str = Field(
        default="SERIALIZABLE",
        description="MERGE transaction isolation: SERIALIZABLE (safest), "
                    "REPEATABLE READ, READ COMMITTED, READ UNCOMMITTED.",
    )
    # NVARCHAR truncation widths applied before the upsert -- defends
    # against "String or binary data would be truncated" errors when
    # an upstream string (verbose model name, long item label) exceeds
    # the column. Single source of truth: ``DEFAULT_STR_LIMITS`` defined
    # at module level above; DbPusher imports the same constant.
    str_limits: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_STR_LIMITS),
    )
    # Hints applied to the MERGE target table. ``HOLDLOCK, UPDLOCK``
    # is Microsoft's recommended pair for atomic MERGE upserts -- holds
    # the key range against inserters and acquires update locks so
    # contending readers don't escalate to deadlocks.
    merge_target_lock_hints: str = Field(
        default="HOLDLOCK, UPDLOCK",
        description="Lock hints applied to MERGE target. Empty disables "
                    "hints entirely (single-writer-per-split deployments).",
    )

    @field_validator("merge_isolation_level")
    @classmethod
    def _validate_isolation(cls, v: str) -> str:
        canon = v.strip().upper().replace("_", " ")
        allowed = {
            "READ UNCOMMITTED", "READ COMMITTED",
            "REPEATABLE READ", "SNAPSHOT", "SERIALIZABLE",
        }
        if canon not in allowed:
            raise ValueError(
                f"merge_isolation_level must be one of {sorted(allowed)}; got {v!r}"
            )
        return canon

    @property
    def configured(self) -> bool:
        return bool(self.host and self.username)

    def connection_string(self) -> str:
        return (
            f"DRIVER={self.driver};"
            f"SERVER={self.host},{self.port};"
            f"DATABASE={self.database};"
            f"UID={self.username};"
            f"PWD={self.password};"
            f"TrustServerCertificate=yes;"
            f"Connection Timeout={self.connection_timeout};"
        )

class Settings(BaseSettings):
    """Server and path settings -- all from env vars with sensible defaults."""

    model_config = {"env_prefix": "DF_", "extra": "ignore"}

    # Server
    app_name: str = Field(default="Demand Forecasting Service")
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8002, ge=1024, le=65535)
    workers: int = Field(default=1, ge=1)
    debug: bool = Field(default=False)
    log_level: str = Field(default="INFO")
    api_prefix: str = Field(default="/api/v1")

    # Pipeline config (YAML path)
    pipeline_config: str = Field(default=str(_PIPELINE_ROOT / "config" / "config.yaml"))

    # Filesystem paths -- everything lives under the unified ``YF_DATA_ROOT``.
    # ``imports/`` holds DB-mirror CSVs (written by data_import); ``forecast/``
    # holds non-DB training artifacts (models, metrics, explainability) that
    # have no SQL representation. Predictions are NOT in ``forecast/`` -- the
    # API reads them from ``imports/demand_forecast.csv`` (DB-canonical via
    # data_import) so the stack has a single source of truth for forecasts.
    raw_data_path: str = Field(default_factory=lambda: str(_data_root() / "imports" / "demand_data_merged.csv"))
    artifacts_dir: str = Field(default_factory=lambda: str(_data_root() / "forecast"))
    models_dir: str = Field(default_factory=lambda: str(_data_root() / "forecast" / "models"))
    predictions_dir: str = Field(default_factory=lambda: str(_data_root() / "forecast" / "predictions"))
    metrics_dir: str = Field(default_factory=lambda: str(_data_root() / "forecast" / "metrics"))
    explainability_dir: str = Field(default_factory=lambda: str(_data_root() / "forecast" / "explainability"))
    logs_dir: str = Field(default_factory=lambda: str(_data_root() / "forecast" / "logs"))

    # Artifact filenames
    test_predictions_file: str = Field(default="test_predictions.csv")
    future_forecast_file: str = Field(default="future_forecast.csv")
    model_metrics_file: str = Field(default="model_metrics.csv")
    training_summary_file: str = Field(default="training_summary.json")
    pair_model_lookup_file: str = Field(default="pair_model_lookup.csv")
    pair_classes_file: str = Field(default="pair_classes.csv")
    pair_explainability_file: str = Field(default="pair_explainability.csv")
    data_quality_file: str = Field(default="data_quality.json")
    # Persisted target-encoding map. Written at training time (from the
    # train window only -- the leakage-free source) and loaded verbatim
    # at inference. Without persistence the encoding silently drifts as
    # new history accumulates between retrains, making inference features
    # diverge from the matrix the model was fit on.
    target_encoding_file: str = Field(default="target_encoding.json")
    # Auxiliary artifact filenames. Surfaced as settings so deployment
    # overrides don't need to track string literals scattered across the
    # codebase. ``outlier_bounds.csv`` is written by training (fit on
    # train_window only) and loaded verbatim at inference. ``conformal_
    # offsets.csv`` is the per-pair calibration map. ``pair_coverage.csv``
    # is the inference-time forecasted/dropped audit.
    outlier_bounds_file: str = Field(default="outlier_bounds.csv")
    conformal_offsets_file: str = Field(default="conformal_offsets.csv")
    pair_coverage_file: str = Field(default="pair_coverage.csv")

    # Outbound HTTP request timeout (van composition pull from
    # data_import). Bounds how long a blocking call to the upstream
    # service can take before a request handler abandons it.
    http_request_timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)

    # Pagination + read limits. Surfaced here so ops can tune memory
    # ceilings without touching code.
    default_page_limit: int = Field(default=5000, ge=1, le=100_000)
    summary_test_predictions_limit: int = Field(default=50_000, ge=1)
    reconciliation_forecast_limit: int = Field(default=10_000, ge=1)
    reconciliation_default_lookback_days: int = Field(default=14, ge=1, le=365)
    reconciliation_min_lookback_days: int = Field(default=1, ge=1)
    reconciliation_max_lookback_days: int = Field(default=90, ge=1, le=365)
    # Drift tolerance for the past-performance items[] sum vs totals
    # identity check. Totals are emitted at 2dp; max accumulated rounding
    # drift across thousands of items is on the order of 0.005 * N, so a
    # 0.5u threshold flags real bugs while staying quiet for the
    # rounding-only case.
    reconciliation_items_drift_threshold: float = Field(default=0.5, ge=0.0)
    # Probability threshold below which a per-day class probability is
    # flagged as "risky" on the van-load page-view's at_risk count.
    # Owned server-side so frontend never re-derives the rule.
    at_risk_prob_threshold: float = Field(default=0.7, ge=0.0, le=1.0)

    # Daily reconciliation refresh cron. Recomputes the four
    # ``yf_demand_forecast`` reconciliation columns (recommended_load,
    # forecast_corrected, bias_pct, opening_stock) for the rolling
    # forecast window using the latest closing_stock + load_allocation
    # values, so the API and ``recommended_order`` can both consume the
    # same pre-computed reconciled van load without recomputing.
    #
    # Schedule (Asia/Dubai by default):
    #   03:00  data_import     -- refreshes closing_stock, load_allocation, ...
    #   03:30  this cron       -- re-reconciles forward window with fresh inputs
    #   04:00  recommended_order generation -- consumes reconciled van load
    # The 30-minute gap absorbs slow nightly imports without crowding the
    # downstream consumer. Override per environment via the env vars.
    reconciliation_refresh_enabled: bool = Field(default=True)
    reconciliation_refresh_timezone: str = Field(default="Asia/Dubai")
    reconciliation_refresh_hour: int = Field(default=3, ge=0, le=23)
    reconciliation_refresh_minute: int = Field(default=30, ge=0, le=59)
    # ``horizon_days_behind`` for the daily cron. Default 1 (= refresh
    # today + yesterday in one simulation pass) eliminates the
    # cross-pass chain drift documented in enrich.py:752 -- adjacent
    # days reconciled by different cron runs see different input states
    # (late invoices, updated forecasts), so the chain identity
    # ``leftover_to_next_day[d] == opening_stock[d+1]`` only holds within
    # a single pass. Setting this to 1+ guarantees the boundary between
    # yesterday and today is always written by the SAME pass, so the
    # chain is internally consistent across the cron cadence.
    # Override per environment via DF_RECONCILIATION_REFRESH_HORIZON_DAYS.
    reconciliation_refresh_horizon_days: int = Field(default=1, ge=0, le=30)

    # Log rotation. The rotating file handler in ``observability.py``
    # reads these so ops can adjust retention without redeploying.
    log_file_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    log_file_backup_count: int = Field(default=5, ge=0)

    # Stale-train threshold for /health/ready. A trained model older
    # than this flips readiness to 503 so a frozen pipeline can't keep
    # serving stale predictions silently.
    stale_train_threshold_seconds: int = Field(default=7 * 24 * 3600, ge=60)
    # Floor on the data_import probe timeout. Readiness probes should
    # fail fast even when the general HTTP timeout is generous.
    health_probe_timeout_seconds: float = Field(default=5.0, gt=0.0, le=60.0)

    # Cascade-call (post-push trigger to data_import). Dataset name and
    # path mirror data_import's registry contract; timeout is the upper
    # bound on how long a pipeline thread waits for the cascade to
    # acknowledge before giving up.
    data_import_dataset: str = Field(default="demand_forecast")
    data_import_path: str = Field(default="/api/v1/data/import")
    data_import_cascade_timeout_seconds: float = Field(default=60.0, gt=0.0, le=600.0)
    training_cascade_lookback_days: int = Field(default=365, ge=30, le=1825,
        description=(
            "Refresh window passed to data_import after a training push. "
            "Training rewrites a rolling block (Test rows back, forecast "
            "horizon forward); pure-append cascade would miss UPDATEs on "
            "dates already mirrored. 365 covers the full Test+Forecast "
            "span with headroom."
        ),
    )

    # Drift-detection window. The retrain scheduler scores the last N
    # calendar days of live predictions vs actual sales to detect drift
    # against the training-time baseline.
    drift_cache_ttl_seconds: int = Field(default=300, ge=0)
    drift_lookback_days: int = Field(default=7, ge=1, le=365)

    # Van-load service caches.
    van_load_max_cache_entries: int = Field(default=500, ge=1)
    van_load_csv_cache_ttl_seconds: int = Field(default=300, ge=0)
    van_load_live_cache_ttl_seconds: int = Field(default=60, ge=0)

    # CORS allow-list -- shared ``YF_ALLOW_ORIGINS`` env var read by
    # every service. Wildcard ``*`` is never used with credentials
    # (browsers reject that combination). Default factory bypasses
    # pydantic-settings' JSON-only env-list parser.
    allow_origins: list[str] = Field(default_factory=_read_allow_origins)

    # ------------------------------------------------------------------
    # Reconciliation layer (V5_b: bias-correct + clamped carry-over)
    # All knobs are dynamic and route- / item-agnostic. There are no
    # per-route or per-item overrides anywhere in the layer -- if a
    # number needs tuning, it lives here.
    # ------------------------------------------------------------------
    # Decision-band ratio thresholds for the "vs typical" allocation
    # label on the reconciliation response. ``ratio = recommended_load /
    # typical_alloc``; below LOW = "LESS", above HIGH = "MORE", else
    # "SAME". Tunable so ops can widen the SAME band without redeploying.
    typical_alloc_band_low: float = Field(default=0.70, gt=0.0, lt=1.0,
        description="Ratio under which the engine flags 'LESS than typical'.")
    typical_alloc_band_high: float = Field(default=1.30, gt=1.0, le=10.0,
        description="Ratio over which the engine flags 'MORE than typical'.")
    bias_lookback_days: int = Field(default=30, ge=7,
        description="Rolling window over which per (route, item) bias is averaged.")
    bias_cap_pct: float = Field(default=0.50, ge=0.05, le=1.0,
        description="Hard cap on |bias| so a single anomalous day cannot dominate.")
    opening_stock_lookback_days: int = Field(default=7, ge=1, le=30,
        description=(
            "Forward-fill window for prev-day closing stock per (route, "
            "item). When closing_stock.csv has a calendar gap (route did "
            "not run that day, or the data pipeline missed a row), the "
            "engine looks back up to this many days for the most recent "
            "closing entry before falling back to opening = 0. Without "
            "this, ~54% of (route, item, day) cells fleet-wide silently "
            "fall to opening = 0 -- the engine then treats the truck as "
            "empty and recommends the full forecast, inflating fresh by "
            "~20%. 7 days handles weekly route schedules cleanly while "
            "keeping long-dormant SKUs (>7 days idle) at opening = 0 "
            "where they belong."
        ),
    )
    calibration_cold_start_ratio: float = Field(default=0.85, ge=0.5, le=1.0,
        description=(
            "Default calibration ratio applied to (route, item) pairs "
            "with no calibration history. Without this, cold-start pairs "
            "fall through to the legacy bias path which produces raw "
            "forecast (no dampening) -- the worst possible behaviour for "
            "the highest-uncertainty rows. 0.85 (15% conservative "
            "shrink) trims fresh issuance on unproven pairs until the "
            "bias service has accumulated enough history. ge=0.5 -- "
            "anything lower would force severe under-loading on "
            "legitimate new SKUs."
        ),
    )
    bias_calibration_cap: float = Field(default=2.0, ge=1.0, le=10.0,
        description=(
            "Upper bound on the per-(route, item) calibration ratio "
            "(sum_actual / sum_predicted, recency-weighted). Without a "
            "cap, a single high-sale day on a sparse-history item can "
            "push the ratio to 5-80x and the engine then multiplies the "
            "model's prediction by that factor for every future day -- "
            "producing tens of thousands of phantom-demand units. 2.0x "
            "leaves a 100% safety buffer above the model and clips the "
            "tail of the ratio distribution. Pairs the cap actually "
            "binds on (~6.7% fleet-wide) are exactly the ones whose raw "
            "ratio is unreliable. ge=1.0 -- a cap below 1.0 would invert "
            "the meaning (forced under-correction)."
        ),
    )
    carry_floor_pct: float = Field(default=0.50, ge=0.0, le=1.0,
        description="Lower clamp: never reduce load below this fraction of the corrected forecast.")

    # ------------------------------------------------------------------
    # Reconciliation v2 -- two adaptive layers on top of the V5_b kernel.
    # Audit on 12 routes 2026-04-27..2026-05-04 measured each layer's
    # marginal contribution; only the layers that pulled weight are
    # kept. All knobs live here, all are env-overridable, all default
    # to the values that produced the best fleet outcome.
    #
    #   L1b -- Pair-maturity bias shrinkage
    #          effective_bias = bias_pct * min(1, n_active / threshold)
    #          Stops noisy bias estimates on low-history pairs from
    #          over-correcting. Cheap, defensive, always-on.
    #
    #   L4  -- Quantile loading (class-aware newsvendor approximation)
    #          target = interpolate(q_low, q50, q_high) at class_quantile
    #          Lumpy and erratic items load at a quantile below the
    #          mean, deliberately trading lost-sales risk for less
    #          overnight stock. Smooth items load at the mean (q50)
    #          -- identical to V5_b.
    # ------------------------------------------------------------------

    # L1b knobs.
    pair_maturity_threshold_days: float = Field(default=14.0, ge=1.0, le=90.0,
        description="Pairs at or above this many sale-days get full bias correction; "
                    "below, correction is shrunk proportionally.")
    maturity_shrinkage_enabled: bool = Field(default=True)

    # L4 knobs (per-class quantile target).
    loading_quantile_smooth: float = Field(default=0.50, ge=0.05, le=0.95)
    loading_quantile_intermittent: float = Field(default=0.40, ge=0.05, le=0.95)
    loading_quantile_erratic: float = Field(default=0.35, ge=0.05, le=0.95)
    loading_quantile_lumpy: float = Field(default=0.30, ge=0.05, le=0.95)
    loading_quantile_default: float = Field(default=0.50, ge=0.05, le=0.95)
    quantile_loading_enabled: bool = Field(default=True)

    # Class-aware bias trim caps. The bias-correction step (forecast_corrected
    # = predicted * (1 - bias_pct), or its calibration-ratio equivalent) can
    # amplify model swings on erratic / lumpy items where the trailing bias
    # is noisy. Capping the |bias_pct| applied for these classes -- and the
    # equivalent deviation of calibration_ratio from 1.0 -- prevents a
    # single noisy window from pushing the corrected forecast far from the
    # historical pattern. Smooth and intermittent items keep the raw bias
    # since their patterns are stable.
    bias_trim_cap_erratic_pct: float = Field(default=0.10, ge=0.0, le=1.0,
        description="Max |bias_pct| applied to demand_class='erratic' rows.")
    bias_trim_cap_lumpy_pct: float = Field(default=0.10, ge=0.0, le=1.0,
        description="Max |bias_pct| applied to demand_class='lumpy' rows.")

    # Sanity-flag thresholds. ``forecast_below_recent`` is set True when the
    # corrected forecast for a row falls below ``forecast_below_recent_factor``
    # of the item's recent per-selling-day average over the last
    # ``forecast_below_recent_window_days`` working days. Both thresholds are
    # read at refresh time so ops can tune them without code changes.
    forecast_below_recent_factor: float = Field(default=0.5, gt=0.0, le=1.0,
        description="Forecast falls below this fraction of recent_avg_per_selling_day -> flag. "
                    "Fallback when the demand_class has no class-specific override.")
    forecast_below_recent_window_days: int = Field(default=28, ge=7, le=365,
        description="Trailing window (working days) used to compute recent per-selling-day average.")

    # Class-aware ``forecast_below_recent`` thresholds. Stable items
    # (smooth / intermittent) should flag earlier because their pattern
    # is reliable -- a 30% drop already signals trouble. Erratic / lumpy
    # items legitimately swing wider so the threshold must be looser to
    # avoid alarm fatigue. The scalar ``forecast_below_recent_factor``
    # above remains the fallback when the row's class is unknown.
    forecast_below_recent_factor_smooth:       float = Field(default=0.7, gt=0.0, le=1.0)
    forecast_below_recent_factor_intermittent: float = Field(default=0.7, gt=0.0, le=1.0)
    forecast_below_recent_factor_erratic:      float = Field(default=0.5, gt=0.0, le=1.0)
    forecast_below_recent_factor_lumpy:        float = Field(default=0.5, gt=0.0, le=1.0)

    # ------------------------------------------------------------------
    # Pattern envelope (class-aware floor/ceiling around recent average).
    # ------------------------------------------------------------------
    # The bias-corrected forecast (``forecast_corrected``) is clipped
    # against ``recent_avg_per_selling_day * factor`` on BOTH sides:
    #   floor   = recent_avg * pattern_floor_factor[class]
    #   ceiling = recent_avg * pattern_ceiling_factor[class]
    #   expected_demand = clip(forecast_corrected, floor, ceiling)
    # The engine then loads against ``expected_demand`` instead of
    # ``forecast_corrected``, so a stable item the model under-shoots
    # (or a wild item the model over-shoots) gets pulled toward its
    # recent pattern. Class-aware because stable items shouldn't deviate
    # as much from their pattern, while erratic / lumpy items have
    # legitimate high variance that a tight envelope would over-clip.
    # All values env-overridable via DF_PATTERN_*_FACTOR_<CLASS>.
    pattern_floor_factor_smooth:       float = Field(default=0.7, ge=0.0, le=1.0)
    pattern_floor_factor_intermittent: float = Field(default=0.6, ge=0.0, le=1.0)
    pattern_floor_factor_erratic:      float = Field(default=0.4, ge=0.0, le=1.0)
    pattern_floor_factor_lumpy:        float = Field(default=0.3, ge=0.0, le=1.0)
    pattern_ceiling_factor_smooth:       float = Field(default=1.5, ge=1.0, le=10.0)
    pattern_ceiling_factor_intermittent: float = Field(default=1.6, ge=1.0, le=10.0)
    pattern_ceiling_factor_erratic:      float = Field(default=2.5, ge=1.0, le=10.0)
    pattern_ceiling_factor_lumpy:        float = Field(default=3.0, ge=1.0, le=10.0)

    # ------------------------------------------------------------------
    # Per-(route, item) z-score envelope (preferred path).
    # ------------------------------------------------------------------
    # The multiplicative class factors above (smooth=0.7..1.5, etc.)
    # apply the SAME width to every item in a class -- ignoring per-pair
    # variance. Two smooth items can have wildly different std; a tight
    # 0.7..1.5 collar on a noisy smooth item over-clips legitimate dips
    # / spikes, and a loose 0.3..3.0 collar on a quiet lumpy item misses
    # outlier days. The z-score envelope replaces those factors with
    #     floor   = max(0, recent_avg - z[class] * recent_std)
    #     ceiling = recent_avg + z[class] * recent_std
    # so the envelope width is driven by the pair's OWN std. Class
    # tuning then lives in the z multiplier alone: stable classes get a
    # tight envelope in std-units (smaller z), volatile classes get a
    # looser one. Pairs with < ``pattern_envelope_min_active_days``
    # selling days in the recent window fall back to the multiplicative
    # factors above -- those remain the cold-start safety net.
    pattern_envelope_z_smooth:       float = Field(default=1.5, ge=0.0, le=10.0)
    pattern_envelope_z_intermittent: float = Field(default=2.0, ge=0.0, le=10.0)
    pattern_envelope_z_erratic:      float = Field(default=2.5, ge=0.0, le=10.0)
    pattern_envelope_z_lumpy:        float = Field(default=3.0, ge=0.0, le=10.0)
    # Minimum selling days observed in the recent window required to
    # trust the per-(route, item) std. Below this we fall back to
    # the multiplicative class factors.
    pattern_envelope_min_active_days: int = Field(default=5, ge=1, le=365)

    # ------------------------------------------------------------------
    # Dormancy guard (zero expected demand for cold (route, item) pairs).
    # ------------------------------------------------------------------
    # When the rep has not sold an item across the last N trip days of a
    # route's journey plan, the engine treats the pair as dormant: its
    # ``expected_demand`` is zeroed BEFORE the leftover-subtraction step
    # so no fresh load is recommended. ``opening_stock`` (carry) still
    # flows through unchanged -- we stop ADDING fresh, not pretend the
    # carry doesn't exist. Universally applied to every (route, item)
    # the engine evaluates; no class gating in v1 (a class-specific
    # threshold knob can be added later if needed).
    dormancy_enabled: bool = Field(default=True)
    dormancy_zero_sale_threshold_trip_days: int = Field(
        default=7, ge=1, le=90,
        description=(
            "A (route, item) is marked dormant if it has zero sales "
            "across the trailing N route-trip days. Reuses the existing "
            "sales_recent + journey_plan indices."
        ),
    )

    def loading_quantile_for_class(self, demand_class: str | None) -> float:
        """Return per-class loading quantile. Falls back to the default
        for unknown / missing classes so a sparse classifier never
        crashes the layer."""
        key = (demand_class or "").strip().lower()
        return {
            "smooth": self.loading_quantile_smooth,
            "intermittent": self.loading_quantile_intermittent,
            "erratic": self.loading_quantile_erratic,
            "lumpy": self.loading_quantile_lumpy,
        }.get(key, self.loading_quantile_default)

    def bias_trim_cap_for_class(self, demand_class: str | None) -> float | None:
        """Return the |bias_pct| cap for a given demand_class, or ``None``
        when the class is trustworthy (smooth / intermittent) and bias
        should pass through unchanged. Driven entirely by the
        ``bias_trim_cap_*_pct`` settings so ops can tune without code
        changes."""
        key = (demand_class or "").strip().lower()
        if key == "erratic":
            return float(self.bias_trim_cap_erratic_pct)
        if key == "lumpy":
            return float(self.bias_trim_cap_lumpy_pct)
        return None

    def pattern_floor_factor_for_class(self, demand_class: str | None) -> float:
        """Multiplier on ``recent_avg_per_selling_day`` for the envelope
        lower bound. Unknown / missing classes use the smooth factor as
        the safe default -- pulling under-shoots up to 70% of recent
        pattern is the most defensive behaviour we'd want for a row we
        cannot otherwise classify."""
        key = (demand_class or "").strip().lower()
        return {
            "smooth":       float(self.pattern_floor_factor_smooth),
            "intermittent": float(self.pattern_floor_factor_intermittent),
            "erratic":      float(self.pattern_floor_factor_erratic),
            "lumpy":        float(self.pattern_floor_factor_lumpy),
        }.get(key, float(self.pattern_floor_factor_smooth))

    def pattern_ceiling_factor_for_class(self, demand_class: str | None) -> float:
        """Multiplier on ``recent_avg_per_selling_day`` for the envelope
        upper bound. Unknown / missing classes fall back to the smooth
        factor (1.5x) -- a tight cap is the safe default; erratic /
        lumpy rows opt into a wider ceiling explicitly via their class
        label."""
        key = (demand_class or "").strip().lower()
        return {
            "smooth":       float(self.pattern_ceiling_factor_smooth),
            "intermittent": float(self.pattern_ceiling_factor_intermittent),
            "erratic":      float(self.pattern_ceiling_factor_erratic),
            "lumpy":        float(self.pattern_ceiling_factor_lumpy),
        }.get(key, float(self.pattern_ceiling_factor_smooth))

    def pattern_envelope_z_for_class(self, demand_class: str | None) -> float:
        """Z multiplier on per-(route, item) recent_std for the z-score
        envelope. Tighter for stable classes, looser for volatile ones.
        Unknown classes use the smooth z (tight) as the safe default --
        a row we can't classify gets the most conservative collar so a
        spurious bias correction can't blow it past a plausible band."""
        key = (demand_class or "").strip().lower()
        return {
            "smooth":       float(self.pattern_envelope_z_smooth),
            "intermittent": float(self.pattern_envelope_z_intermittent),
            "erratic":      float(self.pattern_envelope_z_erratic),
            "lumpy":        float(self.pattern_envelope_z_lumpy),
        }.get(key, float(self.pattern_envelope_z_smooth))

    def forecast_below_recent_factor_for_class(self, demand_class: str | None) -> float:
        """Class-aware threshold for the ``forecast_below_recent`` flag.
        Falls back to the legacy scalar ``forecast_below_recent_factor``
        for unknown / missing classes so a sparse classifier never
        crashes the sanity flag."""
        key = (demand_class or "").strip().lower()
        return {
            "smooth":       float(self.forecast_below_recent_factor_smooth),
            "intermittent": float(self.forecast_below_recent_factor_intermittent),
            "erratic":      float(self.forecast_below_recent_factor_erratic),
            "lumpy":        float(self.forecast_below_recent_factor_lumpy),
        }.get(key, float(self.forecast_below_recent_factor))
    bias_table_file: str = Field(default="bias_table.parquet",
        description="Persisted bias cache; recomputed only when forecast CSV mtime changes.")
    # Shared CSVs produced by data_import that the layer reads.
    closing_stock_file: str = Field(default="closing_stock.csv")
    load_allocation_file: str = Field(default="load_allocation.csv")
    sales_recent_file: str = Field(default="sales_recent.csv")
    returns_recent_file: str = Field(default="returns_recent.csv")
    demand_forecast_file: str = Field(default="demand_forecast.csv")
    sales_transactions_file: str = Field(default="sales_transactions.csv")
    customer_data_file: str = Field(default="customer_data.csv")
    journey_plan_file: str = Field(default="journey_plan.csv")
    shared_data_dir: str = Field(default_factory=lambda: str(_data_root() / "imports"))

    # Journey-aware concentration guard: zero recommended_load on dates
    # the dominant buyer of a whale-driven (route, item) is absent from
    # journey_plan, preventing phantom van capacity no customer can buy.
    concentration_guard_enabled: bool = Field(default=True)
    concentration_threshold: float = Field(default=0.80, ge=0.5, le=1.0,
        description="Min share of recent units top_k buyers must own to flag concentrated.")
    concentration_top_k: int = Field(default=2, ge=1, le=5,
        description="Top-N buyers counted toward share; 2 catches dyadic patterns without ordinary items.")
    concentration_window_days: int = Field(default=90, ge=14, le=365,
        description="Trailing window for buyer-share measurement.")
    concentration_min_units: float = Field(default=50.0, ge=0.0,
        description="Pairs below this volume are skipped -- share is statistical noise.")

    @property
    def data_import_configured(self) -> bool:
        return bool(self.data_import_url)

    def shared_data_path(self, filename: str) -> Path:
        return Path(self.shared_data_dir) / filename

    # Auto-retrain
    retrain_check_interval_hours: int = Field(default=6, ge=1)
    retrain_config_path: str = Field(default=str(_PIPELINE_ROOT / "data" / "retrain_config.json"))
    drift_warn_threshold: float = Field(default=3.0, ge=0)
    drift_alert_threshold: float = Field(default=7.0, ge=0)
    # Maximum number of past auto-retrain run records persisted in
    # retrain_config.json. Older entries are dropped so the file stays
    # small and the rolling-median baseline stays bounded.
    retrain_history_max: int = Field(default=10, ge=2, le=365)
    # Default frequency at which auto-retrain becomes due. Overridden
    # per-deployment via /retrain/config; this is the seed value when
    # the config file is created fresh.
    retrain_default_frequency_days: int = Field(default=14, ge=1, le=365)
    # Convergence tolerance (percentage points) for the rolling-median
    # baseline rotation. A new median within this delta of the current
    # baseline is treated as a no-op so a flat history doesn't churn
    # the persisted file every tick.
    baseline_rotation_convergence_pp: float = Field(default=0.01, ge=0.0, le=10.0)
    # Baseline rotation: once we have at least ``baseline_min_history``
    # successful auto-retrain entries with an ``accuracy_after`` value,
    # the persisted baseline rotates from cold-start ``initialized`` to
    # the median of the trailing ``baseline_history_window`` values.
    # Median (not mean) so a single bad run can't poison the reference;
    # window-bounded so the baseline tracks the model's actual behaviour
    # over time rather than freezing on the day-1 number.
    baseline_history_window: int = Field(default=30, ge=3, le=365)
    baseline_min_history: int = Field(default=5, ge=2)

    # DB push (target table for demand predictions)
    db: DbSettings = Field(default_factory=DbSettings)
    demand_table: str = Field(default="", description="e.g. [YaumiAIML].[dbo].[yf_demand_forecast]")

    # After a successful inference push, optionally call the data_import
    # service so it refreshes ``data/demand_forecast.csv`` from the table
    # we just wrote. Empty -> cascade is skipped (production deployments
    # that orchestrate data_import separately leave this unset).
    data_import_url: str = Field(default="", description="Base URL of the data_import service, e.g. http://localhost:8005")

    # Forward cascade -- POST recommended_order /generate after each
    # reconciliation_refresh. Empty skips the cascade.
    recommended_order_url: str = Field(default="", description="e.g. http://localhost:8001")
    recommended_order_generate_timeout_seconds: float = Field(default=600.0, ge=10.0)

    # YaumiLive (read-only) -- for live actual sales lookup
    live_db_host: str = Field(default="")
    live_db_port: int = Field(default=1433)
    live_db_database: str = Field(default="YaumiLive")
    live_db_username: str = Field(default="")
    live_db_password: str = Field(default="")
    live_sales_view: str = Field(default="[YaumiLive].[dbo].[VW_GET_SALES_DETAILS]")
    live_route_codes: list[str] = Field(default_factory=list)

    @property
    def live_db_configured(self) -> bool:
        return bool(self.live_db_host and self.live_db_username)

    def live_connection_string(self) -> str:
        return (
            f"DRIVER={self.db.driver};"
            f"SERVER={self.live_db_host},{self.live_db_port};"
            f"DATABASE={self.live_db_database};"
            f"UID={self.live_db_username};"
            f"PWD={self.live_db_password};"
            f"TrustServerCertificate=yes;"
            f"Connection Timeout={self.db.connection_timeout};"
        )

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"Invalid log_level: {v}")
        return v

    @field_validator("live_route_codes", mode="before")
    @classmethod
    def _coerce_route_codes(cls, v):
        """Defensive coercion: route codes ship as JSON in some .env layouts
        (``[9105, 9108, ...]``) and pydantic v2 parses bare integers as int.
        The field is typed list[str]; without this, the service refuses to
        boot when env is pre-loaded by a shell wrapper. Stringify so the
        downstream SQL bindings always see strings."""
        if v is None:
            return []
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if x is not None and str(x).strip()]
        return v

    def predictions_path(self, filename: str) -> Path:
        return Path(self.predictions_dir) / filename

    def metrics_path(self, filename: str) -> Path:
        return Path(self.metrics_dir) / filename

    def explainability_path(self, filename: str) -> Path:
        return Path(self.explainability_dir) / filename

    def artifact_path(self, filename: str) -> Path:
        return Path(self.artifacts_dir) / filename

@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env_file = os.getenv("DF_ENV_FILE", ".env")
    if Path(env_file).exists():
        return Settings(_env_file=env_file)
    return Settings()
