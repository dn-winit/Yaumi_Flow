"""API request/response schemas."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ImportRequest(BaseModel):
    dataset: str = Field(description="customer_data | journey_plan | sales_recent | demand_forecast")
    mode: str = Field(default="incremental", description="incremental | full")
    lookback_days: int | None = Field(
        default=None,
        ge=1,
        le=730,
        description="Refresh-window override for incremental mode; re-pulls last N days and dedup-merges. Ignored for full mode.",
    )


class ImportAllRequest(BaseModel):
    mode: str = Field(default="incremental", description="incremental | full")
    lookback_days: int | None = Field(default=None, ge=1, le=730)


class ImportResponse(BaseModel):
    success: bool
    dataset: str = ""
    mode: str = ""
    new_rows: int = 0
    total_rows: int = 0
    file: str = ""
    duration_seconds: float = 0.0
    message: str = ""
    error: str = ""


class ImportAllResponse(BaseModel):
    success: bool
    results: dict[str, Any]


class DatasetInfo(BaseModel):
    exists: bool
    rows: int = 0
    first_date: str | None = None
    last_date: str | None = None
    file: str = ""
    size_mb: float = 0.0


class StatusResponse(BaseModel):
    success: bool
    datasets: dict[str, DatasetInfo]


class HealthResponse(BaseModel):
    status: str
    db_connected: bool
    data_dir: str
    datasets_available: int


class DataSummaryResponse(BaseModel):
    datasets: dict[str, DatasetInfo]
    total_rows: int
    db_connected: bool
    last_updated: str | None = None


# EDA response envelopes -- deep payloads are Dict/List of Any so additive
# fields don't require coordinated backend+frontend deploys. Shapes are
# documented in webapp/src/types/data-import.ts.


class _AvailableEnvelope(BaseModel):
    available: bool
    message: str | None = None


class LastActiveDateResponse(BaseModel):
    """Most recent date with sales activity in sales_recent.csv. Used to
    seed default reporting periods that land on a date with data."""
    available: bool
    date: str | None = None


class SalesOverviewResponse(_AvailableEnvelope):
    # Echo of the (start_date, end_date) the server filtered on (ISO YYYY-MM-DD).
    start_date: str | None = None
    end_date: str | None = None
    totals: dict[str, Any] | None = None
    daily_trend: list[dict[str, Any]] | None = None
    top_routes: list[dict[str, Any]] | None = None
    categories: list[dict[str, Any]] | None = None


class BusinessKpisResponse(_AvailableEnvelope):
    # Echo of the requested window (see SalesOverviewResponse).
    start_date: str | None = None
    end_date: str | None = None
    anchor_date: str | None = None
    working_days: int | None = None
    covered_routes: int | None = None
    covered_days: int | None = None
    total_revenue: dict[str, Any] | None = None
    total_volume: dict[str, Any] | None = None
    unique_items: dict[str, Any] | None = None
    lost_opportunity: dict[str, Any] | None = None


class TrimmedFilterSelections(BaseModel):
    """Selection vector with codes absent from the cascaded option sets
    dropped; frontend applies verbatim so stale codes can't silently
    filter results to nothing."""
    warehouse_codes: list[str] = Field(default_factory=list)
    route_codes: list[str] = Field(default_factory=list)
    category_codes: list[str] = Field(default_factory=list)
    item_codes: list[str] = Field(default_factory=list)


class FilterDimensionsResponse(BaseModel):
    warehouses: list[dict[str, Any]] = Field(default_factory=list)
    routes: list[dict[str, Any]] = Field(default_factory=list)
    categories: list[dict[str, Any]] = Field(default_factory=list)
    items: list[dict[str, Any]] = Field(default_factory=list)
    trimmed_selections: TrimmedFilterSelections = Field(
        default_factory=TrimmedFilterSelections,
    )


class ItemCatalogResponse(BaseModel):
    available: bool
    count: int = 0
    items: list[dict[str, Any]] = Field(default_factory=list)


class ItemStatsResponse(_AvailableEnvelope):
    item_code: str | None = None
    route_code: str | None = None
    anchor_date: str | None = None
    total_transactions: int | None = None
    windows: dict[str, Any] | None = None


class LiveRouteSalesResponse(_AvailableEnvelope):
    route_code: str | None = None
    date: str | None = None
    customers: list[dict[str, Any]] = Field(default_factory=list)
    fetched_at: str | None = None


class LiveCustomerSalesResponse(_AvailableEnvelope):
    route_code: str | None = None
    customer_code: str | None = None
    date: str | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    fetched_at: str | None = None


class LiveVanCompositionResponse(_AvailableEnvelope):
    """Per-item van composition for one (route, date)."""
    route_code: str | None = None
    date: str | None = None
    items: list[dict[str, Any]] = Field(default_factory=list)
    totals: dict[str, Any] = Field(default_factory=dict)
    fetched_at: str | None = None
