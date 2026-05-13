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

        sum(items[*].rep_van_load)         == totals.rep_van_load_total
        sum(items[*].actual_sold)          == totals.actual_sold_total
        sum(items[*].recommended_van_load) == totals.recommended_van_load_total

    ``leftover_to_next_day`` is the carry produced BY this row's day
    (what carries into day + 1). The aggregate
    ``totals.leftover_to_next_day_total`` is the sum of this field over
    items on the LAST active day in the window -- the canonical figure
    the next day's page-view ``carried_qty`` reads.

    ``recommended_carried`` and ``recommended_fresh`` together make up
    ``recommended_van_load`` for the row (carry into the day + fresh
    load this day). ``past_leftover`` and ``today_allocation`` are the
    rep's actual carry + fresh, summing to ``rep_van_load`` for the
    row.

    Envelope / sanity-flag fields (recent_avg_per_selling_day,
    expected_demand, pattern_floor_applied, pattern_ceiling_applied,
    forecast_below_recent, envelope_basis) are NOT carried here. The
    BreakdownPopover that consumes items[] only renders the eight
    numeric carry/fresh/sold/leftover fields above. The same envelope
    diagnostics flow per (route, item, date) via ``table_rows[*].explain``
    on the page-view endpoints, which is the single consumer surface --
    no duplicate wire payload.
    """
    model_config = ConfigDict(extra="forbid")
    itemCode: str
    itemName: str = ""
    date: str
    rep_van_load: float
    past_leftover: float
    today_allocation: float
    recommended_van_load: float
    recommended_carried: float
    recommended_fresh: float
    actual_sold: float
    leftover_to_next_day: float


# Page-view's van-load endpoint emits the same per-(item, date) shape
# scoped to a single date (today). Aliasing keeps the wire contract in
# one place so frontend renderers can share the row component.
VanLoadPageViewItem = PastPerformanceItem


class PastPerformanceResponse(_AvailableEnvelope):
    """Single canonical source for the AccuracyDrawer.

    Carries the three views the drawer needs over one window:
      * ``daily``   -- per-day rows for the 3-line chart
      * ``totals``  -- window aggregates for the KPI tiles (units +
                       holding-cost AED)
      * ``metrics`` -- derived percentages (forecast accuracy, waste %,
                       returns) for the KPI tiles
      * ``items``   -- per-item breakdown over the same window, sorted
                       by ``leftover_to_next_day`` desc so the items
                       responsible for the carry surface first.
    """
    route_code: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    lookback_days: Optional[int] = None
    active_days: Optional[int] = None
    daily: list[dict[str, Any]] = Field(default_factory=list)
    totals: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
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
    """One row in the 'Van load items' table, pre-sorted desc by load.

    Single canonical field per concept. Backend has already substituted
    ``recommended_load`` -> ``units_to_load`` and the canonical bound
    column names -> ``lower_bound`` / ``upper_bound``. The client never
    falls back across names.

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
