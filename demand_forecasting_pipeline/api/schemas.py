"""
Pydantic request/response schemas for the demand forecasting API.

Only models actively wired as ``response_model=`` (or as a nested type
of one) live here. Filter request shapes are expressed as FastAPI
``Query(...)`` declarations directly on the route signatures.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

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
    total_pairs: Optional[int] = None
    classes: dict[str, int] = {}

# ------------------------------------------------------------------
# Pipeline
# ------------------------------------------------------------------

class PipelineRunRequest(BaseModel):
    config_path: Optional[str] = Field(default=None, description="Custom config.yaml path")

class PipelineRunResponse(BaseModel):
    success: bool
    message: str
    config: Optional[str] = None

class PipelineStatusResponse(BaseModel):
    pipeline: str
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_seconds: float = 0.0
    last_success_duration_seconds: Optional[float] = None
    error: Optional[str] = None
    result: dict[str, Any] = {}
    steps: dict[str, str] = {}

# ------------------------------------------------------------------
# Resolved pipeline status -- pre-shaped for the Pipeline page so the
# UI never walks the raw status map or runs worst-of-two reductions.
# ------------------------------------------------------------------

class ResolvedStep(BaseModel):
    """One row in the pipeline progress strip.

    Status, timing fields, and a single metric / detail string per
    step. The UI renders the strings verbatim; the only client-side
    compute that survives is the per-second elapsed-time ticker for a
    running step (presentation animation, not business calculation).
    """
    key: str
    name: str
    status: str  # idle | running | completed | failed | skipped
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    last_success_duration_seconds: Optional[float] = None
    # Plain-language tile lines, computed server-side. ``None`` when
    # the step has nothing honest to surface yet (mid-training counts,
    # missing artefacts, etc.).
    metric_text: Optional[str] = None
    detail_text: Optional[str] = None

class ResolvedPipelineStatus(BaseModel):
    """Composite payload for the Pipeline page's progress strip.

    Each entry in ``steps`` is fully shaped (status + formatted metric
    line + formatted detail line). The publishing step's worst-of-two
    cascade summary is folded into its ``metric_text`` so the UI never
    has to reason about substeps separately.
    """
    success: bool = True
    any_running: bool = False
    steps: list[ResolvedStep] = Field(default_factory=list)

class FutureRouteSummaryRow(BaseModel):
    route_code: str
    skus: int = 0
    # ``predicted_qty`` is the rep's TOTAL van load for the route =
    # ``opening_stock + recommended_load`` summed per item then per
    # route. Same number ``VanLoadSummary`` shows when the route is
    # selected, so the tile and the summary always agree.
    predicted_qty: float = 0.0
    # ``peak_day`` = the day with the highest van-load total within the
    # response window. Equal to the requested date when scoped to one
    # day; surfaces the busiest forward-day when ``date`` is None.
    peak_day: Optional[str] = None

class FutureRouteSummaryResponse(BaseModel):
    success: bool = True
    date: Optional[str] = None
    routes: list[FutureRouteSummaryRow] = []
    # True when the V5_b reconciliation engine produced reconciled
    # values for every row that fed this response. False indicates a
    # degraded mode (bias table missing / cold start) -- the UI shows
    # a warning chip on the tiles so a dispatcher knows the numbers
    # reflect raw forecast, not the reconciled van load.
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
    # ``accuracy_pct`` and ``total_pairs`` are intentionally nullable: a
    # numeric zero would render as "0%" / "0 pairs" in the UI, which is
    # misleading while training is still in flight. ``None`` lets the UI
    # show "-" until the artifacts that back these numbers actually exist.
    accuracy_pct: Optional[float] = None
    total_pairs: Optional[int] = None
    classes: dict[str, int] = {}
    test_predictions_count: int = 0
    future_forecast_count: int = 0
    last_forecast_date: Optional[str] = None
    training_summary_exists: bool = False
    training_overview: Optional[dict[str, Any]] = None

# ------------------------------------------------------------------
# Retrain config / history
# ------------------------------------------------------------------

class RetrainConfigResponse(BaseModel):
    """Mirrors AutoRetrainConfig._data + the live drift assessment.

    Field names match retrain_config.json on disk and the webapp's
    `RetrainConfig` interface so the JSON round-trips through the API
    without renaming.
    """
    enabled: bool = False
    frequency_days: int = 14
    auto_inference_after_train: bool = True
    last_auto_retrain: Optional[str] = None
    next_scheduled: Optional[str] = None
    history: list[dict[str, Any]] = Field(default_factory=list)
    drift: Optional[dict[str, Any]] = None

    model_config = {"extra": "ignore"}

class RetrainHistoryResponse(BaseModel):
    history: list[dict[str, Any]] = Field(default_factory=list)

# ------------------------------------------------------------------
# Reconciliation
# ------------------------------------------------------------------

class _AvailableEnvelope(BaseModel):
    available: bool
    message: Optional[str] = None

class VanLoadResponse(_AvailableEnvelope):
    """Per-item van composition for one (route, date)."""
    route_code: Optional[str] = None
    date: Optional[str] = None
    source: Optional[str] = None    # "live" | "csv"
    items: list[dict[str, Any]] = Field(default_factory=list)
    totals: dict[str, Any] = Field(default_factory=dict)
    fetched_at: Optional[str] = None

class ReconciliationResponse(_AvailableEnvelope):
    """V5_b recommendations joined with the actual van composition."""
    route_code: Optional[str] = None
    date: Optional[str] = None
    source: Optional[str] = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    totals: dict[str, Any] = Field(default_factory=dict)
    fetched_at: Optional[str] = None

class PastPerformanceItem(BaseModel):
    """Per-(item, date) breakdown row for the past-performance window.

    Each row is keyed by (itemCode, date), so the table renders one line
    per item per active day in the window. Sums across rows reconcile
    with the aggregate ``totals`` block within the rounding tolerance
    (``reconciliation_items_drift_threshold``):

        sum(items[*].rep_van_load)          == totals.rep_van_load_total
        sum(items[*].recommended_van_load)  == totals.recommended_van_load_total
        sum(items[*].actual_sold)           == totals.actual_sold_total
        sum(items[*].actual_leftover)       == totals.rep_leftover_units
        sum(items[*].recommended_leftover)  == totals.our_leftover_units

    Leftovers are the naive "what's still on the truck at end of day"
    figure -- ``max(load - sold, 0)`` -- one per policy. Items that ran
    short (sold > load) are stock-outs, not leftovers, so the field is
    bounded at 0.
    """
    model_config = ConfigDict(extra="forbid")
    itemCode: str
    itemName: str = ""
    categoryName: str = ""
    date: str
    rep_van_load: float
    recommended_van_load: float
    actual_sold: float
    # Leftovers under each policy -- naive max(load - sold, 0).
    # Identity: sum across items_payload equals the matching totals.
    actual_leftover: float
    recommended_leftover: float


# Page-view's van-load endpoint emits the same per-(item, date) shape
# scoped to a single date (today), extended with the rep's actual
# loading numbers sourced from yf_sales_transactions.yaumi_*. These
# come from VW_GET_CLOSING_STOCK + VW_GET_LOAD_ALLOCATION_DETAILS via
# reconciliation_refresh, and let the frontend show the rep's process
# alongside ours per item without a second fetch.
#
# Optional because past dates predating the yaumi_* backfill, or future
# dates with no rep activity, surface NULL on the DB row. The frontend
# treats None as "no rep data" and renders an em-dash.
class VanLoadPageViewItem(PastPerformanceItem):
    yaumi_opening_stock: Optional[float] = None
    yaumi_fresh_load: Optional[float] = None
    yaumi_total_van_load: Optional[float] = None
    yaumi_leftover: Optional[float] = None
    # Dormancy guard flag from yf_sales_transactions.forecast_dormant.
    # True when the (route, item) pair had zero sales across its route's
    # last N trip days and the engine zeroed expected_demand for it.
    # Backwards-compatible: pre-existing DB rows surface NULL.
    forecast_dormant: Optional[bool] = None


class PastPerformanceCategoryRow(BaseModel):
    """Per-category rollup across the whole past-performance window.

    One row per ``categoryName`` aggregated from ``items_payload`` --
    identity-preserving by construction (``sum(categories[*].field)``
    equals ``sum(items[*].field)`` for every numeric field below).

    ``skus`` is the count of distinct itemCodes inside the category
    that had ANY activity (load, recommendation, or sale) across the
    window. Categories with all-zero rows are filtered server-side.
    """
    model_config = ConfigDict(extra="forbid")
    categoryName: str
    skus: int
    rep_van_load: float
    recommended_van_load: float
    actual_sold: float
    actual_leftover: float
    recommended_leftover: float


class PastPerformanceResponse(_AvailableEnvelope):
    """Single canonical source for the AccuracyDrawer.

    The drawer shows three hero numbers + a one-line insight banner +
    a daily comparison chart + a category rollup + a per-item table.
    Everything is server-pre-computed; the client renders verbatim.

      * ``daily``          -- per-day rows for the chart
      * ``totals``         -- window aggregates for the hero tiles
      * ``categories``     -- per-category rollup for the (collapsible)
                              category breakdown table
      * ``items``          -- per-(item, date) breakdown for the
                              (collapsible) item-by-item table
    """
    route_code: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    lookback_days: Optional[int] = None
    active_days: Optional[int] = None
    daily: list[dict[str, Any]] = Field(default_factory=list)
    totals: dict[str, Any] = Field(default_factory=dict)
    categories: list[PastPerformanceCategoryRow] = Field(default_factory=list)
    items: list[PastPerformanceItem] = Field(default_factory=list)

# ------------------------------------------------------------------
# Page views -- one fetch per page state, fully pre-computed payload
# ------------------------------------------------------------------
#
# The webapp is a render layer. Tile, chart, and table on the same page
# read from one of these objects so they cannot disagree -- everything
# is computed from one snapshot of the source frame, in one Python
# process, in one request.
#
# ASCII-only: no smart quotes, em-dashes, mathematical symbols, or
# other non-ASCII bytes anywhere below this line.

class VanLoadSummaryView(BaseModel):
    """KPI tile values for the VanLoad summary row.

    Server-enforced identity (cross-checked in the handler):
        van_load_qty   == carried_qty + issued_qty
        van_load_items == count of distinct ItemCodes that contributed
                          to either carried or issued.
    ``revenue`` is None when no row in the load-this set has a price;
    ``has_revenue`` is the explicit flag so the client never tests for
    null.
    """
    van_load_qty: float = 0.0
    van_load_items: int = 0
    carried_qty: float = 0.0
    carried_items: int = 0
    issued_qty: float = 0.0
    issued_items: int = 0
    revenue: Optional[float] = None
    has_revenue: bool = False
    at_risk: int = 0

class VanLoadChartItem(BaseModel):
    """One bar in the 'Top N items by van load' chart, pre-sorted desc."""
    item_code: str
    item_name: str
    predicted: float

class VanLoadTableRow(BaseModel):
    """One row in the 'Van load items' table, pre-sorted desc by total
    truck weight. Carry-aware: rows with ``opening_stock > 0`` and zero
    fresh load survive (the leftover is real physical inventory).

    Single canonical field per concept. Backend has already substituted
    ``recommended_load`` -> ``units_to_load`` and the canonical bound
    column names -> ``lower_bound`` / ``upper_bound``. The client never
    falls back across names.

    Two distinct quantity fields:

      * ``units_to_load``         = fresh allocation the engine recommends
                                    the depot ISSUE today (post-V5_b).
                                    Ceil to integer (pack reality).
      * ``recommended_van_load``  = TOTAL truck weight for the item =
                                    ``ceil(opening_stock) + units_to_load``.
                                    The headline number the modal shows.

    Both are needed so the modal can show the math chain transparently:
    today's fresh load (engine) + yesterday's leftover (carry) = total
    truck weight.

    ``has_real_confidence`` is the server's verdict on whether p_demand
    is a real probability (intermittent/lumpy two-stage classes) vs a
    synthetic 0/1 fallback (smooth/erratic). The badge component just
    reads this flag.

    ``explain`` carries engine intermediates used only by the
    explainability popup, not rendered by the table itself.
    """
    item_code: str
    item_name: str
    units_to_load: float
    recommended_van_load: float
    p_demand: Optional[float] = None
    demand_class: Optional[str] = None
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None
    has_real_confidence: bool = False
    explain: dict[str, Any] = Field(default_factory=dict)

class VanLoadPageView(BaseModel):
    """Composite payload for the VanLoad route-detail page.

    One HTTP fetch per (route, date). The page binds directly to this
    object -- no client-side aggregation, no client-side sort, no
    client-side field fallbacks.
    """
    success: bool = True
    available: bool = True
    message: Optional[str] = None
    route_code: str
    date: str
    reconciled: bool = True
    summary: VanLoadSummaryView = Field(default_factory=VanLoadSummaryView)
    chart_top_n: list[VanLoadChartItem] = Field(default_factory=list)
    table_rows: list[VanLoadTableRow] = Field(default_factory=list)
    # Per-(item, date) breakdown -- one row per item for today's date
    # (the queried ``date``). Same shape as ``PastPerformanceItem`` so
    # the page-view's popovers can render in-memory from this payload
    # without an extra fetch to /reconciliation/past-performance.
    items: list[VanLoadPageViewItem] = Field(default_factory=list)


# ------------------------------------------------------------------
# ForecastDrawer page view (Upcoming plan)
# ------------------------------------------------------------------

class ForecastDrawerSummary(BaseModel):
    """KPI tile values for the Upcoming-plan drawer.

    Server-enforced: total_van_load == sum(chart_data[*].predicted) and
    horizon_days == len(chart_data). The window label fields are the
    first / last dates in chart_data; ``line_count`` is the table size.
    """
    horizon_days: int = 0
    total_van_load: float = 0.0
    skus: int = 0
    avg_per_day: float = 0.0
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    line_count: int = 0

class ForecastDrawerChartPoint(BaseModel):
    """One date in the daily van-load chart, sorted ascending by date.

    ``q10`` / ``q90`` are populated only when ``show_band`` is true on
    the parent (single-SKU view) -- quantiles are not additive across
    SKUs. Multi-SKU view emits ``None`` so the frontend can drop the
    band layer cleanly instead of rendering a misleading flat-zero band.
    """
    date: str
    predicted: float
    q10: Optional[float] = None
    q90: Optional[float] = None

class ForecastDrawerTableRow(BaseModel):
    """One line in the line-item table, sorted asc by date then desc by
    units_to_load. Same field contract as VanLoadTableRow but carries a
    ``date`` so the table can break by day."""
    date: str
    item_code: str
    item_name: str
    units_to_load: float
    p_demand: Optional[float] = None
    demand_class: Optional[str] = None
    lower_bound: Optional[float] = None
    upper_bound: Optional[float] = None
    has_real_confidence: bool = False
    explain: dict[str, Any] = Field(default_factory=dict)

class ForecastDrawerView(BaseModel):
    """Composite payload for the Upcoming-plan drawer.

    One HTTP fetch covers: tiles (horizon, total, avg-per-day, SKUs),
    daily chart series, line-item table, and the show_band flag that
    decides whether the chart renders a confidence ribbon.
    """
    success: bool = True
    available: bool = True
    message: Optional[str] = None
    route_code: Optional[str] = None
    item_codes: list[str] = Field(default_factory=list)
    from_date: str
    show_band: bool = False
    reconciled: bool = True
    summary: ForecastDrawerSummary = Field(default_factory=ForecastDrawerSummary)
    chart_data: list[ForecastDrawerChartPoint] = Field(default_factory=list)
    table_rows: list[ForecastDrawerTableRow] = Field(default_factory=list)
