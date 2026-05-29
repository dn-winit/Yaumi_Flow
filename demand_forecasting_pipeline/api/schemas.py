"""Pydantic request/response schemas for the demand forecasting API.

Only models wired as ``response_model=`` (or nested types of one) live here;
request filters are FastAPI ``Query(...)`` decls on the route signatures.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ------------------------------------------------------------------
# Predictions
# ------------------------------------------------------------------

class PredictionResponse(BaseModel):
    success: bool
    source: str  # "test_predictions" or "future_forecast"
    total: int
    data: list[dict[str, Any]]

# ------------------------------------------------------------------
# Metrics
# ------------------------------------------------------------------

class MetricsResponse(BaseModel):
    success: bool
    total: int
    data: list[dict[str, Any]]

# ------------------------------------------------------------------
# Explainability
# ------------------------------------------------------------------

class ClassSummaryResponse(BaseModel):
    success: bool
    total_pairs: int | None = None
    classes: dict[str, int] = {}

# ------------------------------------------------------------------
# Pipeline
# ------------------------------------------------------------------

class PipelineRunRequest(BaseModel):
    """Trigger a pipeline run; ``config_path`` (if given) must resolve to a YAML
    file inside the deployed pipeline config dir. Prevents arbitrary-file-read."""
    config_path: str | None = Field(
        default=None,
        description="Custom config YAML path inside deployed pipeline config dir.",
    )

    @field_validator("config_path")
    @classmethod
    def _validate_config_path(cls, v: str | None) -> str | None:
        if v is None:
            return v
        # Lazy import keeps schemas cheap for doc tooling.
        from demand_forecasting_pipeline.config.settings import get_settings
        s = get_settings()
        try:
            base = Path(s.pipeline_config).resolve(strict=False).parent
            target = Path(v).resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            # Generic message; absolute paths are info-disclosure pre-auth.
            # ``from None`` suppresses the wrapped exception so the chain
            # doesn't surface filesystem internals to the caller.
            raise ValueError("config_path is not a valid filesystem path") from None
        if target.suffix.lower() not in (".yaml", ".yml"):
            raise ValueError("config_path must be a .yaml or .yml file")
        # Containment check; do NOT echo base dir to caller.
        try:
            target.relative_to(base)
        except ValueError:
            raise ValueError(
                "config_path must resolve inside the deployed pipeline "
                "config directory"
            ) from None
        return str(target)

class PipelineRunResponse(BaseModel):
    success: bool
    message: str
    config: str | None = None

class PipelineStatusResponse(BaseModel):
    pipeline: str
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float = 0.0
    last_success_duration_seconds: float | None = None
    error: str | None = None
    result: dict[str, Any] = {}
    steps: dict[str, str] = {}

# Resolved pipeline status -- pre-shaped for the Pipeline page progress strip.

class ResolvedStep(BaseModel):
    """One row in the pipeline progress strip; UI renders strings verbatim."""
    key: str
    name: str
    status: str  # idle | running | completed | failed | skipped
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    last_success_duration_seconds: float | None = None
    # Plain-language tile lines, server-computed; ``None`` until honest data exists.
    metric_text: str | None = None
    detail_text: str | None = None

class ResolvedPipelineStatus(BaseModel):
    """Composite payload for the Pipeline page; each step fully shaped server-side."""
    success: bool = True
    any_running: bool = False
    steps: list[ResolvedStep] = Field(default_factory=list)

class FutureRouteSummaryRow(BaseModel):
    route_code: str
    skus: int = 0
    # Rep's TOTAL van load for the route = opening_stock + recommended_load, summed.
    predicted_qty: float = 0.0
    # Day with the highest van-load total in the response window; busiest forward-day when date=None.
    peak_day: str | None = None

class FutureRouteSummaryResponse(BaseModel):
    success: bool = True
    date: str | None = None
    routes: list[FutureRouteSummaryRow] = []
    # False indicates degraded mode (bias missing / cold start); UI shows a warning chip.
    reconciled: bool = True

# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------

class ArtifactStatus(BaseModel):
    test_predictions: bool = False
    future_forecast: bool = False
    model_metrics: bool = False
    training_summary: bool = False
    pair_model_lookup: bool = False
    pair_classes: bool = False
    pair_explainability: bool = False

class HealthResponse(BaseModel):
    status: str
    artifacts: ArtifactStatus
    pipelines: dict[str, str]
    config_path: str
    cache_keys: list[str] = []

# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------

class ForecastSummaryResponse(BaseModel):
    # accuracy_pct / total_pairs nullable: numeric zero renders misleadingly mid-training;
    # None lets the UI show "-" until artifacts exist.
    accuracy_pct: float | None = None
    total_pairs: int | None = None
    classes: dict[str, int] = {}
    test_predictions_count: int = 0
    future_forecast_count: int = 0
    last_forecast_date: str | None = None
    training_summary_exists: bool = False
    training_overview: dict[str, Any] | None = None

# ------------------------------------------------------------------
# Retrain config / history
# ------------------------------------------------------------------

class RetrainConfigResponse(BaseModel):
    """Mirrors AutoRetrainConfig._data + live drift; field names match disk + webapp."""
    enabled: bool = False
    frequency_days: int = 14
    auto_inference_after_train: bool = True
    last_auto_retrain: str | None = None
    next_scheduled: str | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)
    drift: dict[str, Any] | None = None

    model_config = {"extra": "ignore"}

class RetrainHistoryResponse(BaseModel):
    history: list[dict[str, Any]] = Field(default_factory=list)

# Model versioning -- /retrain/versions and /retrain/rollback

class ModelVersionEntry(BaseModel):
    """One version-registry row; mirrors manifest.json on disk.

    ``is_current`` flags the current.json pointer. Champion-challenger gate fields
    (decision, champion_accuracy, delta_pp) are None on pre-Phase-6 versions."""
    version_id: str
    path: str
    promoted_at: str | None = None
    promoted_by: str | None = None
    trigger: str | None = None
    accuracy_before: float | None = None
    accuracy_after: float | None = None
    duration_seconds: float | None = None
    is_current: bool = False
    decision: str | None = None
    champion_accuracy: float | None = None
    delta_pp: float | None = None


class ModelVersionsResponse(BaseModel):
    total: int
    current_version_id: str | None = None
    versions: list[ModelVersionEntry] = Field(default_factory=list)


class RollbackRequest(BaseModel):
    version_id: str


class RollbackResponse(BaseModel):
    success: bool
    restored: ModelVersionEntry

# ------------------------------------------------------------------
# Reconciliation
# ------------------------------------------------------------------

class _AvailableEnvelope(BaseModel):
    available: bool
    message: str | None = None

class VanLoadResponse(_AvailableEnvelope):
    """Per-item van composition for one (route, date)."""
    route_code: str | None = None
    date: str | None = None
    source: str | None = None    # "live" | "csv"
    items: list[dict[str, Any]] = Field(default_factory=list)
    totals: dict[str, Any] = Field(default_factory=dict)
    fetched_at: str | None = None

class ReconciliationResponse(_AvailableEnvelope):
    """V5_b recommendations joined with the actual van composition."""
    route_code: str | None = None
    date: str | None = None
    source: str | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    totals: dict[str, Any] = Field(default_factory=dict)
    fetched_at: str | None = None

class PastPerformanceItem(BaseModel):
    """Per-(item, date) breakdown row; sums reconcile with ``totals`` within
    ``reconciliation_items_drift_threshold``.

    Identities:
        sum(items[*].rep_van_load)          == totals.rep_van_load_total
        sum(items[*].recommended_van_load)  == totals.recommended_van_load_total
        sum(items[*].actual_sold)           == totals.actual_sold_total
        sum(items[*].actual_leftover)       == totals.rep_leftover_units
        sum(items[*].recommended_leftover)  == totals.our_leftover_units

    Leftover = max(load - sold, 0); stock-outs (sold > load) are clamped to 0.
    """
    model_config = ConfigDict(extra="forbid")
    itemCode: str
    itemName: str = ""
    categoryName: str = ""
    date: str
    rep_van_load: float
    recommended_van_load: float
    actual_sold: float
    # Per-policy leftover = max(load - sold, 0); sum across items == matching totals.
    actual_leftover: float
    recommended_leftover: float


# Page-view's van-load endpoint emits the same shape scoped to a single date,
# extended with rep's actual loading from yf_sales_transactions.yaumi_*. Optional
# because past dates / no-activity dates surface NULL.
class VanLoadPageViewItem(PastPerformanceItem):
    yaumi_opening_stock: float | None = None
    yaumi_fresh_load: float | None = None
    yaumi_total_van_load: float | None = None
    yaumi_leftover: float | None = None
    # Dormancy flag from yf_sales_transactions.forecast_dormant; pre-existing rows surface NULL.
    forecast_dormant: bool | None = None


class PastPerformanceCategoryRow(BaseModel):
    """Per-category rollup; sum(categories[*].field) == sum(items[*].field)."""
    model_config = ConfigDict(extra="forbid")
    categoryName: str
    skus: int
    rep_van_load: float
    recommended_van_load: float
    actual_sold: float
    actual_leftover: float
    recommended_leftover: float


class PastPerformanceResponse(_AvailableEnvelope):
    """Single canonical source for the AccuracyDrawer; client renders verbatim.

    daily: per-day rows for the chart.
    totals: window aggregates for hero tiles.
    categories: per-category rollup table.
    items: per-(item, date) breakdown table.
    """
    route_code: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    lookback_days: int | None = None
    active_days: int | None = None
    daily: list[dict[str, Any]] = Field(default_factory=list)
    totals: dict[str, Any] = Field(default_factory=dict)
    categories: list[PastPerformanceCategoryRow] = Field(default_factory=list)
    items: list[PastPerformanceItem] = Field(default_factory=list)

# Page views -- one fetch per page state, fully pre-computed payload.
# Tile/chart/table on the same page read from one object; ASCII-only below.

class VanLoadSummaryView(BaseModel):
    """KPI tile values for the VanLoad summary row.

    Server-enforced identity (cross-checked in the handler):
        van_load_qty   == carried_qty + issued_qty
        van_load_items == count of distinct ItemCodes contributing to either side.
    ``has_revenue`` is the explicit flag; client never tests revenue for null.
    """
    van_load_qty: float = 0.0
    van_load_items: int = 0
    carried_qty: float = 0.0
    carried_items: int = 0
    issued_qty: float = 0.0
    issued_items: int = 0
    revenue: float | None = None
    has_revenue: bool = False
    at_risk: int = 0

class VanLoadChartItem(BaseModel):
    """One bar in the 'Top N items by van load' chart, pre-sorted desc."""
    item_code: str
    item_name: str
    predicted: float

class VanLoadTableRow(BaseModel):
    """One row in 'Van load items' table, pre-sorted desc by total truck weight.

    Carry-aware: rows with opening_stock>0 and zero fresh load survive (real inventory).

      * units_to_load        = fresh allocation engine recommends issuing today (ceil int).
      * recommended_van_load = ceil(opening_stock) + units_to_load (headline number).

    has_real_confidence: True for intermittent/lumpy two-stage; False for smooth/erratic
    synthetic 0/1 fallback. explain carries engine intermediates for popup only.
    """
    item_code: str
    item_name: str
    units_to_load: float
    recommended_van_load: float
    p_demand: float | None = None
    demand_class: str | None = None
    lower_bound: float | None = None
    upper_bound: float | None = None
    has_real_confidence: bool = False
    explain: dict[str, Any] = Field(default_factory=dict)

class VanLoadPageView(BaseModel):
    """Composite payload for the VanLoad route-detail page; one fetch per (route, date).

    Page binds directly: no client-side aggregation, sort, or field fallbacks.
    """
    success: bool = True
    available: bool = True
    message: str | None = None
    route_code: str
    date: str
    reconciled: bool = True
    summary: VanLoadSummaryView = Field(default_factory=VanLoadSummaryView)
    chart_top_n: list[VanLoadChartItem] = Field(default_factory=list)
    table_rows: list[VanLoadTableRow] = Field(default_factory=list)
    # Per-(item, date) rows for the queried date; same shape as PastPerformanceItem
    # so popovers render in-memory without a second fetch.
    items: list[VanLoadPageViewItem] = Field(default_factory=list)


# ForecastDrawer page view (Upcoming plan)

class ForecastDrawerSummary(BaseModel):
    """KPI tiles for Upcoming-plan drawer.

    Server-enforced: total_van_load == sum(chart_data[*].predicted),
    horizon_days == len(chart_data); window_* are first/last chart dates.
    """
    horizon_days: int = 0
    total_van_load: float = 0.0
    skus: int = 0
    avg_per_day: float = 0.0
    window_start: str | None = None
    window_end: str | None = None
    line_count: int = 0

class ForecastDrawerChartPoint(BaseModel):
    """One date in the daily van-load chart, asc-sorted.

    q10/q90 populated only when parent.show_band is True (single-SKU view);
    quantiles are not additive across SKUs, so multi-SKU emits None.
    """
    date: str
    predicted: float
    q10: float | None = None
    q90: float | None = None

class ForecastDrawerTableRow(BaseModel):
    """One line, sorted asc by date then desc by units_to_load; same contract as
    VanLoadTableRow plus ``date`` for day-breaks."""
    date: str
    item_code: str
    item_name: str
    units_to_load: float
    p_demand: float | None = None
    demand_class: str | None = None
    lower_bound: float | None = None
    upper_bound: float | None = None
    has_real_confidence: bool = False
    explain: dict[str, Any] = Field(default_factory=dict)

class ForecastDrawerView(BaseModel):
    """Composite Upcoming-plan drawer payload: tiles, chart series, line-item table, show_band."""
    success: bool = True
    available: bool = True
    message: str | None = None
    route_code: str | None = None
    item_codes: list[str] = Field(default_factory=list)
    from_date: str
    show_band: bool = False
    reconciled: bool = True
    summary: ForecastDrawerSummary = Field(default_factory=ForecastDrawerSummary)
    chart_data: list[ForecastDrawerChartPoint] = Field(default_factory=list)
    table_rows: list[ForecastDrawerTableRow] = Field(default_factory=list)
