"""Application settings from env vars. Pipeline ML params live in config.yaml."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from common.sales_vocab import SALES_VOCAB as _VOCAB
from common.settings_base import data_root as _shared_data_root
from common.settings_base import read_allow_origins as _read_allow_origins

_PIPELINE_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _PIPELINE_ROOT.parent


def _data_root() -> Path:
    return _shared_data_root(_PROJECT_ROOT)


# NVARCHAR widths matching scripts/create_tables.sql + migrations; single source of truth for DbPusher.
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
    # Cursor timeout for bulk forecast push; larger than connect handshake budget.
    query_timeout: int = Field(default=300, ge=10)
    retry_attempts: int = Field(default=3, ge=1)
    retry_delay: int = Field(default=2, ge=1)
    # Rows per executemany batch; large enough to amortise round-trip cost.
    executemany_chunk_size: int = Field(default=1000, ge=1)
    # SERIALIZABLE prevents the MERGE phantom-row race when two writers target the same key set.
    merge_isolation_level: str = Field(
        default="SERIALIZABLE",
        description="MERGE transaction isolation level.",
    )
    # NVARCHAR truncation widths applied before upsert; defends against truncation errors.
    str_limits: dict[str, int] = Field(
        default_factory=lambda: dict(DEFAULT_STR_LIMITS),
    )
    # HOLDLOCK+UPDLOCK is MS's recommended pair for atomic MERGE upserts.
    merge_target_lock_hints: str = Field(
        default="HOLDLOCK, UPDLOCK",
        description="Lock hints for MERGE target; empty disables hints.",
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
        # Treat placeholder values (``your-db-server`` / ``your_username``
        # shipped in .env.example) as "not configured" so cron writes skip
        # cleanly instead of crashing on pyodbc.connect.
        from common.runtime import is_placeholder_value
        if is_placeholder_value(self.host) or is_placeholder_value(self.username):
            return False
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
    """Server and path settings, all env-driven with sensible defaults."""

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

    # Filesystem paths under YF_DATA_ROOT. ``imports/`` holds DB-mirror CSVs; ``forecast/`` holds
    # non-DB training artifacts. Predictions live in ``imports/demand_forecast.csv`` (DB-canonical),
    # not in ``forecast/``. Raw-data path is in config.yaml::paths.raw_data, not here.
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
    # Target-encoding map fit on train window only; persistence prevents drift between retrains.
    target_encoding_file: str = Field(default="target_encoding.json")
    # Per-pair earliest-observed date; keeps ``periods_since_start`` date-anchored across inference windows.
    pair_origins_file: str = Field(default="pair_origins.json")
    # Aux artifacts: outlier bounds (train-fit), conformal offsets, inference forecast/dropped audit.
    outlier_bounds_file: str = Field(default="outlier_bounds.csv")
    conformal_offsets_file: str = Field(default="conformal_offsets.csv")
    pair_coverage_file: str = Field(default="pair_coverage.csv")

    # Upper bound on blocking HTTP calls (van composition pull from data_import).
    http_request_timeout_seconds: float = Field(default=30.0, gt=0.0, le=300.0)

    # Pagination + read limits, env-tunable.
    default_page_limit: int = Field(default=5000, ge=1, le=100_000)
    summary_test_predictions_limit: int = Field(default=50_000, ge=1)
    reconciliation_forecast_limit: int = Field(default=10_000, ge=1)
    reconciliation_default_lookback_days: int = Field(default=14, ge=1, le=365)
    reconciliation_min_lookback_days: int = Field(default=1, ge=1)
    reconciliation_max_lookback_days: int = Field(default=90, ge=1, le=365)
    # Drift tolerance for items[] sum vs totals identity (totals emitted at 2dp).
    reconciliation_items_drift_threshold: float = Field(default=0.5, ge=0.0)
    # Probability threshold below which a per-day class probability is "risky" (server-owned rule).
    at_risk_prob_threshold: float = Field(default=0.7, ge=0.0, le=1.0)

    # Daily reconciliation refresh cron. Schedule (Asia/Dubai default):
    #   03:00 data_import; 03:30 this cron; 04:00 recommended_order generation.
    reconciliation_refresh_enabled: bool = Field(default=True)
    reconciliation_refresh_timezone: str = Field(default="Asia/Dubai")
    reconciliation_refresh_hour: int = Field(default=3, ge=0, le=23)
    reconciliation_refresh_minute: int = Field(default=30, ge=0, le=59)

    # Auto-cascade controls: one POST /pipeline/train can drive pre-train data refresh,
    # train+inference, and post-inference reconciliation; each is opt-out via env.
    auto_data_refresh_before_train: bool = Field(default=True)
    auto_data_refresh_lookback_days: int = Field(default=30, ge=1, le=730)
    auto_reconcile_after_inference: bool = Field(default=True)
    auto_reconcile_horizon_days: int = Field(default=30, ge=1, le=365)
    # Rolling refresh window. Default 30 days lets the cron self-heal YaumiLive late-posts
    # (returns/closing-stock corrections) without manual /refresh. Ceiling 90 for ops backfills.
    reconciliation_refresh_horizon_days: int = Field(default=30, ge=0, le=90)
    # Freeze model-side columns (recommended_load, opening_stock, leftover_to_next_day,
    # forecast_corrected, bias_pct, etc.) for past dates. Without this, a retrain
    # would silently rewrite yesterday's "what we recommended" history -- past-performance
    # drawer shows different numbers for the same date before vs after retrain.
    # Rep-side columns (actual_sold, yaumi_*) keep updating so late returns still flow in.
    # Turn off for legitimate full-history backfills.
    reconciliation_freeze_past_model_cols: bool = Field(default=True)
    # Dedup window for the cron-context (force=False) reconciliation backstop.
    # If a successful refresh ran within this many seconds, the cron tick is
    # a no-op. Default 30min matches the 03:00 cascade -> 03:30 backstop gap.
    reconciliation_cron_dedup_window_seconds: int = Field(default=1800, ge=60, le=86400)
    # Cascade window padding: the data_import refresh after a reconciliation
    # write covers ``max(horizon_days_behind + pad, min_days)``. Smaller pad
    # means tighter mirror refresh; larger gives more catch-up on late-arriving
    # rows. Defaults preserve the prior hardcoded ``max(horizon+2, 7)``.
    reconciliation_cascade_lookback_pad_days:  int = Field(default=2,  ge=0, le=30)
    reconciliation_cascade_lookback_min_days:  int = Field(default=7,  ge=1, le=90)

    # YaumiLive transaction-type vocab. Defaults come from the shared
    # ``common.sales_vocab.SALES_VOCAB`` so this service and data_import
    # can't drift on the strings; env overrides (DF_*_TRX_TYPE) still work.
    sales_item_type:         str = Field(default=_VOCAB.sales_item_type)
    sales_invoice_trx_type:  str = Field(default=_VOCAB.sales_invoice_trx_type)
    bad_return_trx_type:     str = Field(default=_VOCAB.bad_return_trx_type)
    good_return_trx_type:    str = Field(default=_VOCAB.good_return_trx_type)

    # Log rotation knobs read by observability.py.
    log_file_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    log_file_backup_count: int = Field(default=5, ge=0)
    # IANA zone for log timestamps. Default Asia/Dubai matches every other
    # service in the stack (cron schedulers, supervision tick TZ) so log
    # timestamps and cron-audit rows correlate without a +1.5h offset.
    log_timezone: str = Field(default="Asia/Dubai")

    # Stale-train threshold for /health/ready; flips readiness to 503 past this age.
    stale_train_threshold_seconds: int = Field(default=7 * 24 * 3600, ge=60)
    # Floor on data_import probe timeout so readiness fails fast.
    health_probe_timeout_seconds: float = Field(default=5.0, gt=0.0, le=60.0)

    # Cascade call (post-push trigger to data_import); mirrors data_import's registry contract.
    data_import_dataset: str = Field(default="demand_forecast")
    data_import_path: str = Field(default="/api/v1/data/import")
    data_import_cascade_timeout_seconds: float = Field(default=60.0, gt=0.0, le=600.0)
    training_cascade_lookback_days: int = Field(default=365, ge=30, le=1825,
        description=(
            "Refresh window for post-train cascade; must cover the full "
            "Test+Forecast span to catch UPDATEs."
        ),
    )

    # Drift-detection window: scheduler scores last N days of live vs actuals against baseline.
    drift_cache_ttl_seconds: int = Field(default=300, ge=0)
    drift_lookback_days: int = Field(default=7, ge=1, le=365)

    # Van-load service caches.
    van_load_max_cache_entries: int = Field(default=500, ge=1)
    van_load_csv_cache_ttl_seconds: int = Field(default=300, ge=0)
    van_load_live_cache_ttl_seconds: int = Field(default=60, ge=0)
    # Short TTL for csv-fallback responses so next read switches back when data_import recovers.
    van_load_csv_fallback_cache_ttl_seconds: int = Field(default=10, ge=0,
        description="TTL for csv-fallback responses; short by design.")

    # Refuse to reconcile if newest forecast row is older than this many days.
    forecast_stale_threshold_days: int = Field(default=14, ge=1, le=180,
        description="Refuse to reconcile if MAX(yf_demand_forecast.trx_date) is older than this.")

    # CORS allow-list -- shared YF_ALLOW_ORIGINS read by every service. No ``*`` with credentials.
    allow_origins: list[str] = Field(default_factory=_read_allow_origins)

    # ------------------------------------------------------------------
    # Reconciliation layer (V5_b: bias-correct + clamped carry-over).
    # All knobs are global; no per-route / per-item overrides exist.
    # ------------------------------------------------------------------
    # "vs typical" decision band: ratio = recommended_load / typical_alloc;
    # below LOW = LESS, above HIGH = MORE, else SAME.
    typical_alloc_band_low: float = Field(default=0.70, gt=0.0, lt=1.0,
        description="Ratio under which the engine flags 'LESS than typical'.")
    typical_alloc_band_high: float = Field(default=1.30, gt=1.0, le=10.0,
        description="Ratio over which the engine flags 'MORE than typical'.")
    bias_lookback_days: int = Field(default=30, ge=7,
        description="Rolling window over which per (route, item) bias is averaged.")
    bias_cap_pct: float = Field(default=0.50, ge=0.05, le=1.0,
        description="Hard cap on |bias| so a single anomalous day cannot dominate.")
    # CSV-mirror pre-refresh window for /reconciliation/refresh; must cover
    # bias_lookback_days back + full forecast horizon forward.
    forecast_csv_refresh_lookback_days: int = Field(default=60, ge=14, le=365)
    opening_stock_lookback_days: int = Field(default=7, ge=1, le=30,
        description=(
            "Forward-fill window for prev-day closing stock per (route, item). "
            "Handles weekly route schedules; keeps long-dormant SKUs at opening=0."
        ),
    )
    calibration_cold_start_ratio: float = Field(default=0.85, ge=0.5, le=1.0,
        description=(
            "Default calibration ratio for pairs with no history; 0.85 trims "
            "fresh issuance on unproven pairs until bias accumulates."
        ),
    )
    bias_calibration_cap: float = Field(default=2.0, ge=1.0, le=10.0,
        description=(
            "Upper bound on per-(route, item) calibration ratio; clips tail of "
            "the ratio distribution to prevent phantom-demand inflation."
        ),
    )
    carry_floor_pct: float = Field(default=0.50, ge=0.0, le=1.0,
        description="Lower clamp: never reduce load below this fraction of the corrected forecast.")

    # ------------------------------------------------------------------
    # Reconciliation v2: adaptive layers on top of V5_b kernel.
    #   L1b -- Pair-maturity bias shrinkage (always-on, defensive).
    #   L4  -- Quantile loading (class-aware newsvendor approximation).
    # ------------------------------------------------------------------

    # L1b knobs.
    pair_maturity_threshold_days: float = Field(default=14.0, ge=1.0, le=90.0,
        description="Pairs at or above this many sale-days get full bias correction; below, shrunk.")
    maturity_shrinkage_enabled: bool = Field(default=True)

    # L4 knobs (per-class quantile target).
    loading_quantile_smooth: float = Field(default=0.50, ge=0.05, le=0.95)
    loading_quantile_intermittent: float = Field(default=0.40, ge=0.05, le=0.95)
    loading_quantile_erratic: float = Field(default=0.35, ge=0.05, le=0.95)
    loading_quantile_lumpy: float = Field(default=0.30, ge=0.05, le=0.95)
    loading_quantile_default: float = Field(default=0.50, ge=0.05, le=0.95)
    quantile_loading_enabled: bool = Field(default=True)

    # Class-aware bias trim caps: only erratic/lumpy get capped; smooth/intermittent pass through.
    bias_trim_cap_erratic_pct: float = Field(default=0.10, ge=0.0, le=1.0,
        description="Max |bias_pct| applied to erratic rows.")
    bias_trim_cap_lumpy_pct: float = Field(default=0.10, ge=0.0, le=1.0,
        description="Max |bias_pct| applied to lumpy rows.")

    # Sanity flag: forecast_below_recent fires when corrected forecast < factor * recent avg.
    forecast_below_recent_factor: float = Field(default=0.5, gt=0.0, le=1.0,
        description="Fallback factor when class has no class-specific override.")
    forecast_below_recent_window_days: int = Field(default=28, ge=7, le=365,
        description="Trailing window (working days) for recent per-selling-day average.")

    # Class-aware below-recent thresholds; stable classes flag earlier, volatile ones looser.
    forecast_below_recent_factor_smooth:       float = Field(default=0.7, gt=0.0, le=1.0)
    forecast_below_recent_factor_intermittent: float = Field(default=0.7, gt=0.0, le=1.0)
    forecast_below_recent_factor_erratic:      float = Field(default=0.5, gt=0.0, le=1.0)
    forecast_below_recent_factor_lumpy:        float = Field(default=0.5, gt=0.0, le=1.0)

    # ------------------------------------------------------------------
    # Pattern envelope: clip forecast_corrected against recent_avg * factor on both sides.
    # expected_demand = clip(forecast_corrected, floor, ceiling); class-aware widths.
    # ------------------------------------------------------------------
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
    # floor/ceiling = recent_avg -/+ z[class] * recent_std; falls back to
    # multiplicative class factors when active days < min threshold.
    # ------------------------------------------------------------------
    pattern_envelope_z_smooth:       float = Field(default=1.5, ge=0.0, le=10.0)
    pattern_envelope_z_intermittent: float = Field(default=2.0, ge=0.0, le=10.0)
    pattern_envelope_z_erratic:      float = Field(default=2.5, ge=0.0, le=10.0)
    pattern_envelope_z_lumpy:        float = Field(default=3.0, ge=0.0, le=10.0)
    # Below this many selling days, std is untrustworthy; revert to class factors.
    pattern_envelope_min_active_days: int = Field(default=5, ge=1, le=365)

    # ------------------------------------------------------------------
    # Dormancy guard: zero expected_demand for cold (route, item) pairs BEFORE
    # leftover subtraction so no fresh load is added; opening_stock still flows.
    # ------------------------------------------------------------------
    dormancy_enabled: bool = Field(default=True)
    dormancy_zero_sale_threshold_trip_days: int = Field(
        default=7, ge=1, le=90,
        description="Mark (route, item) dormant if zero sales over trailing N route-trip days.",
    )

    def loading_quantile_for_class(self, demand_class: str | None) -> float:
        """Per-class loading quantile; falls back to default for unknown classes."""
        key = (demand_class or "").strip().lower()
        return {
            "smooth": self.loading_quantile_smooth,
            "intermittent": self.loading_quantile_intermittent,
            "erratic": self.loading_quantile_erratic,
            "lumpy": self.loading_quantile_lumpy,
        }.get(key, self.loading_quantile_default)

    def bias_trim_cap_for_class(self, demand_class: str | None) -> float | None:
        """|bias_pct| cap per class; ``None`` for smooth/intermittent (pass through)."""
        key = (demand_class or "").strip().lower()
        if key == "erratic":
            return float(self.bias_trim_cap_erratic_pct)
        if key == "lumpy":
            return float(self.bias_trim_cap_lumpy_pct)
        return None

    def pattern_floor_factor_for_class(self, demand_class: str | None) -> float:
        """Envelope lower-bound multiplier on recent_avg; smooth factor is the safe default."""
        key = (demand_class or "").strip().lower()
        return {
            "smooth":       float(self.pattern_floor_factor_smooth),
            "intermittent": float(self.pattern_floor_factor_intermittent),
            "erratic":      float(self.pattern_floor_factor_erratic),
            "lumpy":        float(self.pattern_floor_factor_lumpy),
        }.get(key, float(self.pattern_floor_factor_smooth))

    def pattern_ceiling_factor_for_class(self, demand_class: str | None) -> float:
        """Envelope upper-bound multiplier on recent_avg; smooth (1.5x) is the safe default."""
        key = (demand_class or "").strip().lower()
        return {
            "smooth":       float(self.pattern_ceiling_factor_smooth),
            "intermittent": float(self.pattern_ceiling_factor_intermittent),
            "erratic":      float(self.pattern_ceiling_factor_erratic),
            "lumpy":        float(self.pattern_ceiling_factor_lumpy),
        }.get(key, float(self.pattern_ceiling_factor_smooth))

    def pattern_envelope_z_for_class(self, demand_class: str | None) -> float:
        """Z multiplier on per-(route, item) recent_std; smooth (tight) is the safe default."""
        key = (demand_class or "").strip().lower()
        return {
            "smooth":       float(self.pattern_envelope_z_smooth),
            "intermittent": float(self.pattern_envelope_z_intermittent),
            "erratic":      float(self.pattern_envelope_z_erratic),
            "lumpy":        float(self.pattern_envelope_z_lumpy),
        }.get(key, float(self.pattern_envelope_z_smooth))

    def forecast_below_recent_factor_for_class(self, demand_class: str | None) -> float:
        """Class-aware threshold for forecast_below_recent flag; falls back to scalar setting."""
        key = (demand_class or "").strip().lower()
        return {
            "smooth":       float(self.forecast_below_recent_factor_smooth),
            "intermittent": float(self.forecast_below_recent_factor_intermittent),
            "erratic":      float(self.forecast_below_recent_factor_erratic),
            "lumpy":        float(self.forecast_below_recent_factor_lumpy),
        }.get(key, float(self.forecast_below_recent_factor))
    bias_table_file: str = Field(default="bias_table.parquet",
        description="Persisted bias cache; recomputed only when forecast CSV mtime changes.")
    # Shared CSVs produced by data_import.
    closing_stock_file: str = Field(default="closing_stock.csv")
    load_allocation_file: str = Field(default="load_allocation.csv")
    sales_recent_file: str = Field(default="sales_recent.csv")
    returns_recent_file: str = Field(default="returns_recent.csv")
    demand_forecast_file: str = Field(default="demand_forecast.csv")
    sales_transactions_file: str = Field(default="sales_transactions.csv")
    customer_data_file: str = Field(default="customer_data.csv")
    journey_plan_file: str = Field(default="journey_plan.csv")
    shared_data_dir: str = Field(default_factory=lambda: str(_data_root() / "imports"))

    # Journey-aware concentration guard: zero recommended_load on dates the dominant buyer
    # of a whale-driven (route, item) is absent from journey_plan.
    concentration_guard_enabled: bool = Field(default=True)
    concentration_threshold: float = Field(default=0.80, ge=0.5, le=1.0,
        description="Min share of recent units top_k buyers must own to flag concentrated.")
    concentration_top_k: int = Field(default=2, ge=1, le=5,
        description="Top-N buyers counted toward share.")
    concentration_window_days: int = Field(default=90, ge=14, le=365,
        description="Trailing window for buyer-share measurement.")
    concentration_min_units: float = Field(default=50.0, ge=0.0,
        description="Pairs below this volume are skipped (share is noise).")

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
    # Max retained past auto-retrain run records (bounds rolling-median baseline).
    retrain_history_max: int = Field(default=10, ge=2, le=365)
    # Seed value when retrain_config.json is created fresh; overridden via /retrain/config.
    retrain_default_frequency_days: int = Field(default=14, ge=1, le=365)
    # Convergence tolerance (pp) for rolling-median baseline rotation; prevents churn on flat history.
    baseline_rotation_convergence_pp: float = Field(default=0.01, ge=0.0, le=10.0)
    # Baseline rotates from cold-start to trailing median once min_history accuracy_after entries exist.
    # Median (not mean) to resist single bad runs; window-bounded to track drift over time.
    baseline_history_window: int = Field(default=30, ge=3, le=365)
    baseline_min_history: int = Field(default=5, ge=2)

    # Drift-accelerated auto-retrain. Cooldown scales with operator-selected frequency_days:
    #   cooldown_days = max(retrain_cooldown_min_days, round(frequency_days * retrain_cooldown_fraction))
    retrain_on_drift_alert_enabled: bool = Field(default=True)
    retrain_cooldown_fraction: float = Field(default=0.25, ge=0.0, le=1.0,
        description="Cooldown after a retrain, as a fraction of frequency_days.")
    retrain_cooldown_min_days: int = Field(default=1, ge=1, le=30,
        description="Absolute floor on the drift-accelerator cooldown.")

    # Model versioning -- artifact registry retention.
    # ModelRegistry snapshots live artifacts after each retrain; oldest pruned beyond cap,
    # active version always preserved. Default 10 ~ 20 weeks at 14-day cadence.
    model_retention_max_versions: int = Field(default=10, ge=1, le=100,
        description="Max versioned artifact snapshots under versions/; oldest pruned, active preserved.")

    # Scheduler leader-election lock path. One worker per host acquires this and starts
    # cron schedulers; others run as followers. Filesystem-scoped (multi-host needs distributed lease).
    scheduler_lock_path: str = Field(
        default_factory=lambda: str(_data_root() / "forecast" / "scheduler.lock"),
        description="Filesystem path for scheduler leader-election lock.",
    )

    # Drop predictions whose trx_date is within N days of today; same-day invoices still in flight.
    # Default 2 covers typical invoice-upload SLA; set to 0 for dev fixtures.
    accuracy_settlement_window_days: int = Field(default=2, ge=0, le=14,
        description="Drop in-flight predictions within this many days of today from accuracy scoring.")

    # Champion-challenger promotion gate: reject auto-retrains that regress beyond threshold;
    # snapshot still recorded, but current.json pointer held back. 1.0pp absorbs natural variance.
    champion_challenger_enabled: bool = Field(default=True,
        description="When True, reject auto-retrains that regress beyond challenger_max_regression_pp.")
    challenger_max_regression_pp: float = Field(default=1.0, ge=0.0, le=100.0,
        description="Max allowed accuracy regression (pp) for a challenger to be promoted.")

    # DB push (target table for demand predictions)
    db: DbSettings = Field(default_factory=DbSettings)
    demand_table: str = Field(default="", description="e.g. [YaumiAIML].[dbo].[yf_demand_forecast]")

    # Post-push cascade to data_import to refresh demand_forecast.csv. Empty disables.
    data_import_url: str = Field(default="", description="Base URL of the data_import service.")

    # Forward cascade -- POST recommended_order /generate after reconciliation_refresh. Empty disables.
    recommended_order_url: str = Field(default="", description="e.g. http://localhost:8001")
    recommended_order_generate_timeout_seconds: float = Field(default=600.0, ge=10.0)

    # Forward cascade -- POST sales_supervision /session/internal/invalidate-day after
    # reconciliation_refresh so open supervisor sessions drop their stale rec snapshot
    # and the next reconcile tick rebuilds from the fresh DB state. Empty disables.
    sales_supervision_url: str = Field(default="", description="e.g. http://localhost:8004")
    sales_supervision_cascade_timeout_seconds: float = Field(default=10.0, ge=1.0)

    # YaumiLive (read-only) for live actual sales lookup
    live_db_host: str = Field(default="")
    live_db_port: int = Field(default=1433)
    live_db_database: str = Field(default="YaumiLive")
    live_db_username: str = Field(default="")
    live_db_password: str = Field(default="")
    live_sales_view: str = Field(default="[YaumiLive].[dbo].[VW_GET_SALES_DETAILS]")
    live_route_codes: list[str] = Field(default_factory=list)

    @property
    def live_db_configured(self) -> bool:
        from common.runtime import is_placeholder_value
        if is_placeholder_value(self.live_db_host) or is_placeholder_value(self.live_db_username):
            return False
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

    @field_validator("log_timezone")
    @classmethod
    def _validate_log_timezone(cls, v: str) -> str:
        """Reject unknown IANA zone at boot via zoneinfo lookup."""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"Invalid log_timezone {v!r}: not a known IANA zone "
                f"(zoneinfo lookup failed: {exc})"
            ) from exc
        return v

    @field_validator("live_route_codes", mode="before")
    @classmethod
    def _coerce_route_codes(cls, v):
        """Coerce shell-loaded route-code lists (incl. bare ints) to list[str]."""
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
