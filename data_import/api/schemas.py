"""API request/response schemas."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ImportRequest(BaseModel):
    dataset: str = Field(description="customer_data | journey_plan | sales_recent | demand_forecast")
    mode: str = Field(default="incremental", description="incremental | full")
    lookback_days: Optional[int] = Field(
        default=None,
        ge=1,
        le=730,
        description="Refresh-window override for incremental mode; re-pulls last N days and dedup-merges. Ignored for full mode.",
    )


class ImportAllRequest(BaseModel):
    mode: str = Field(default="incremental", description="incremental | full")
    lookback_days: Optional[int] = Field(default=None, ge=1, le=730)


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
    results: Dict[str, Any]


class DatasetInfo(BaseModel):
    exists: bool
    rows: int = 0
    first_date: Optional[str] = None
    last_date: Optional[str] = None
    file: str = ""
    size_mb: float = 0.0


class StatusResponse(BaseModel):
    success: bool
    datasets: Dict[str, DatasetInfo]


class HealthResponse(BaseModel):
    status: str
    db_connected: bool
    data_dir: str
    datasets_available: int


class DataSummaryResponse(BaseModel):
    datasets: Dict[str, DatasetInfo]
    total_rows: int
    db_connected: bool
    last_updated: Optional[str] = None


# EDA response envelopes -- deep payloads are Dict/List of Any so additive
# fields don't require coordinated backend+frontend deploys. Shapes are
# documented in webapp/src/types/data-import.ts.


class _AvailableEnvelope(BaseModel):
    available: bool
    message: Optional[str] = None


class LastActiveDateResponse(BaseModel):
    """Most recent date with sales activity in sales_recent.csv. Used to
    seed default reporting periods that land on a date with data."""
    available: bool
    date: Optional[str] = None


class SalesOverviewResponse(_AvailableEnvelope):
    # Echo of the (start_date, end_date) the server filtered on (ISO YYYY-MM-DD).
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    totals: Optional[Dict[str, Any]] = None
    daily_trend: Optional[List[Dict[str, Any]]] = None
    top_routes: Optional[List[Dict[str, Any]]] = None
    categories: Optional[List[Dict[str, Any]]] = None


class BusinessKpisResponse(_AvailableEnvelope):
    # Echo of the requested window (see SalesOverviewResponse).
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    anchor_date: Optional[str] = None
    working_days: Optional[int] = None
    covered_routes: Optional[int] = None
    covered_days: Optional[int] = None
    total_revenue: Optional[Dict[str, Any]] = None
    total_volume: Optional[Dict[str, Any]] = None
    unique_items: Optional[Dict[str, Any]] = None
    lost_opportunity: Optional[Dict[str, Any]] = None


class TrimmedFilterSelections(BaseModel):
    """Selection vector with codes absent from the cascaded option sets
    dropped; frontend applies verbatim so stale codes can't silently
    filter results to nothing."""
    warehouse_codes: List[str] = Field(default_factory=list)
    route_codes: List[str] = Field(default_factory=list)
    category_codes: List[str] = Field(default_factory=list)
    item_codes: List[str] = Field(default_factory=list)


class FilterDimensionsResponse(BaseModel):
    warehouses: List[Dict[str, Any]] = Field(default_factory=list)
    routes: List[Dict[str, Any]] = Field(default_factory=list)
    categories: List[Dict[str, Any]] = Field(default_factory=list)
    items: List[Dict[str, Any]] = Field(default_factory=list)
    trimmed_selections: TrimmedFilterSelections = Field(
        default_factory=TrimmedFilterSelections,
    )


class ItemCatalogResponse(BaseModel):
    available: bool
    count: int = 0
    items: List[Dict[str, Any]] = Field(default_factory=list)


class ItemStatsResponse(_AvailableEnvelope):
    item_code: Optional[str] = None
    route_code: Optional[str] = None
    anchor_date: Optional[str] = None
    total_transactions: Optional[int] = None
    windows: Optional[Dict[str, Any]] = None


class LiveRouteSalesResponse(_AvailableEnvelope):
    route_code: Optional[str] = None
    date: Optional[str] = None
    customers: List[Dict[str, Any]] = Field(default_factory=list)
    fetched_at: Optional[str] = None


class LiveCustomerSalesResponse(_AvailableEnvelope):
    route_code: Optional[str] = None
    customer_code: Optional[str] = None
    date: Optional[str] = None
    items: List[Dict[str, Any]] = Field(default_factory=list)
    fetched_at: Optional[str] = None


class LiveVanCompositionResponse(_AvailableEnvelope):
    """Per-item van composition for one (route, date)."""
    route_code: Optional[str] = None
    date: Optional[str] = None
    items: List[Dict[str, Any]] = Field(default_factory=list)
    totals: Dict[str, Any] = Field(default_factory=dict)
    fetched_at: Optional[str] = None
