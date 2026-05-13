"""
API routes for data import.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from data_import.api.dependencies import get_eda_service, get_importer
from data_import.api.schemas import (
    BusinessKpisResponse,
    DataSummaryResponse,
    DatasetInfo,
    FilterDimensionsResponse,
    HealthResponse,
    ImportAllRequest,
    ImportAllResponse,
    ImportRequest,
    ImportResponse,
    ItemCatalogResponse,
    ItemStatsResponse,
    LiveCustomerSalesResponse,
    LiveRouteSalesResponse,
    LastActiveDateResponse,
    LiveVanCompositionResponse,
    SalesOverviewResponse,
    StatusResponse,
)
from data_import.config.settings import get_settings
from data_import.core.importer import DataImporter
from data_import.services.eda_service import EdaService

router = APIRouter()


@router.post("/import", response_model=ImportResponse)
def import_dataset(
    req: ImportRequest,
    importer: DataImporter = Depends(get_importer),
    eda: EdaService = Depends(get_eda_service),
):
    """Import a single dataset (incremental or full).

    Cache invalidation runs in a finally so that any partial import which
    still touched the CSV (rows written before a downstream raise) does
    not leave aggregations serving stale data.
    """
    result: dict = {}
    try:
        result = importer.import_dataset(
            req.dataset, req.mode, lookback_days=req.lookback_days,
        )
        return ImportResponse(**result)
    finally:
        if result.get("new_rows", 0) > 0:
            eda.invalidate()


@router.post("/import-all", response_model=ImportAllResponse)
def import_all(
    req: ImportAllRequest = ImportAllRequest(),
    importer: DataImporter = Depends(get_importer),
    eda: EdaService = Depends(get_eda_service),
):
    """Import all datasets. Same try/finally invariant as /import."""
    results: dict = {}
    try:
        results = importer.import_all(req.mode, lookback_days=req.lookback_days)
        success = all(r.get("success", False) for r in results.values())
        return ImportAllResponse(success=success, results=results)
    finally:
        if any(r.get("new_rows", 0) > 0 for r in results.values()):
            eda.invalidate()


@router.get("/status", response_model=StatusResponse)
def data_status(importer: DataImporter = Depends(get_importer)):
    """Show current state of all local data files."""
    info = importer.status()
    datasets = {k: DatasetInfo(**v) for k, v in info.items()}
    return StatusResponse(success=True, datasets=datasets)


@router.get("/summary", response_model=DataSummaryResponse)
def data_summary(importer: DataImporter = Depends(get_importer)):
    """Aggregated KPI summary across all datasets."""
    info = importer.status()
    datasets = {k: DatasetInfo(**v) for k, v in info.items()}
    total_rows = sum(d.rows for d in datasets.values())
    last_dates = [d.last_date for d in datasets.values() if d.last_date]
    last_updated = max(last_dates) if last_dates else None
    return DataSummaryResponse(
        datasets=datasets,
        total_rows=total_rows,
        db_connected=importer.test_connection(),
        last_updated=last_updated,
    )


@router.get("/health", response_model=HealthResponse)
def health(importer: DataImporter = Depends(get_importer)):
    settings = get_settings()
    db_ok = importer.test_connection()
    info = importer.status()
    available = sum(1 for v in info.values() if v.get("exists", False))
    return HealthResponse(
        status="healthy" if db_ok else "degraded",
        db_connected=db_ok,
        data_dir=settings.data_dir,
        datasets_available=available,
    )


_DATE_RE = r"^\d{4}-\d{2}-\d{2}$"


@router.get("/eda/sales", response_model=SalesOverviewResponse)
def eda_sales(
    start_date: str = Query(..., pattern=_DATE_RE, description="Inclusive lower bound (YYYY-MM-DD)"),
    end_date:   str = Query(..., pattern=_DATE_RE, description="Inclusive upper bound (YYYY-MM-DD), >= start_date"),
    warehouse_codes: List[str] = Query(default=[], alias="warehouse_codes"),
    route_codes: List[str] = Query(default=[], alias="route_codes"),
    category_codes: List[str] = Query(default=[], alias="category_codes"),
    item_codes: List[str] = Query(default=[], alias="item_codes"),
    svc: EdaService = Depends(get_eda_service),
):
    """Aggregated EDA over sales_recent.csv: totals, daily trend, top items, top routes, categories.

    Slice = ``[start_date, end_date]`` inclusive. Dates with no activity
    contribute zero (the chart still renders the X-axis tick) so a
    weekend / holiday inside the window is visible as a gap, not silently
    folded out. FilterBar selections are honoured together against the
    same windowed slice.
    """
    return svc.get_sales_overview(
        start_date, end_date, warehouse_codes, route_codes, category_codes, item_codes,
    )


@router.get("/eda/items", response_model=ItemCatalogResponse)
def eda_items(svc: EdaService = Depends(get_eda_service)):
    """Item catalog: item_code, name, category, avg_price, last_price, total_quantity."""
    return svc.get_item_catalog()


@router.get("/eda/last-active-date", response_model=LastActiveDateResponse)
def eda_last_active_date(svc: EdaService = Depends(get_eda_service)):
    """Most recent date in sales_recent.csv. Drawers (van-load past
    performance, recommendation adoption) call this on open to seed
    defaults that always land on a date the data actually covers,
    instead of hardcoding "yesterday" -- which on a weekend / holiday
    would surface the empty-state every Monday morning."""
    return svc.get_last_active_date()


@router.get("/eda/business-kpis", response_model=BusinessKpisResponse)
def eda_business_kpis(
    start_date: str = Query(..., pattern=_DATE_RE, description="Inclusive lower bound (YYYY-MM-DD)"),
    end_date:   str = Query(..., pattern=_DATE_RE, description="Inclusive upper bound (YYYY-MM-DD), >= start_date"),
    warehouse_codes: List[str] = Query(default=[], alias="warehouse_codes"),
    route_codes: List[str] = Query(default=[], alias="route_codes"),
    category_codes: List[str] = Query(default=[], alias="category_codes"),
    item_codes: List[str] = Query(default=[], alias="item_codes"),
    svc: EdaService = Depends(get_eda_service),
):
    """Four executive-impact KPIs computed from a single (recs ⋈ sales) join:

        1. SKU coverage  -- items predicted ∩ items sold
        2. Field adoption -- recommended lines that hit invoices
        3. Revenue running on recommendations
        4. Peer uplift potential -- AED if every route adopted at the
           rate of the top quartile

    All four respect the FilterBar + the active reporting period
    ``[start_date, end_date]``, and are evaluated only over (route, date)
    cells that actually have rec coverage.
    """
    return svc.get_business_kpis(
        start_date, end_date, warehouse_codes, route_codes, category_codes, item_codes,
    )


@router.get("/eda/filter-dimensions", response_model=FilterDimensionsResponse)
def eda_filter_dimensions(
    warehouse_codes: List[str] = Query(default=[], alias="warehouse_codes"),
    route_codes: List[str] = Query(default=[], alias="route_codes"),
    category_codes: List[str] = Query(default=[], alias="category_codes"),
    item_codes: List[str] = Query(default=[], alias="item_codes"),
    svc: EdaService = Depends(get_eda_service),
):
    """Cascading filter options for the dashboard FilterBar.

    Each downstream dimension is the set of unique values present in the
    sales slice that already matches every upstream selection. Items are
    filtered by all three upstream levels. ``trimmed_selections`` returns
    the same input vector with any codes no longer present in the
    cascaded option sets dropped, so the FilterBar applies the cleaned
    selection without re-validating client-side.
    """
    return svc.get_filter_dimensions(
        warehouse_codes, route_codes, category_codes, item_codes,
    )


# ------------------------------------------------------------------
# Live YaumiLive cut-throughs. Consumed cross-service by the supervision
# microservice (LiveActualsClient) so the supervisor's "Mark visited"
# flow can pull real actuals without a direct DB grant. 60-second cache
# absorbs rapid-fire visit clicks on the same (route, date) cell.
# ------------------------------------------------------------------


@router.get("/eda/live-route-sales", response_model=LiveRouteSalesResponse)
def eda_live_route_sales(
    route_code: str = Query(..., description="Route code"),
    date: str = Query(..., pattern=_DATE_RE, description="YYYY-MM-DD"),
    svc: EdaService = Depends(get_eda_service),
):
    return svc.get_live_route_sales(route_code, date)


@router.get("/eda/live-customer-sales", response_model=LiveCustomerSalesResponse)
def eda_live_customer_sales(
    route_code: str = Query(..., description="Route code"),
    date: str = Query(..., pattern=_DATE_RE, description="YYYY-MM-DD"),
    customer_code: str = Query(..., description="Customer code"),
    svc: EdaService = Depends(get_eda_service),
):
    return svc.get_live_customer_sales(route_code, date, customer_code)


@router.get("/eda/live-van-composition", response_model=LiveVanCompositionResponse)
def eda_live_van_composition(
    route_code: str = Query(..., description="Route code"),
    date: str = Query(..., pattern=_DATE_RE, description="YYYY-MM-DD"),
    svc: EdaService = Depends(get_eda_service),
):
    """Reconstructed van state for one (route, date): per item -- past
    leftover, today's allocation, total van load, sold, bad/good returns,
    leftover-now, and end-of-day closing if posted. 60-s cached.
    """
    return svc.get_live_van_composition(route_code, date)


@router.get("/eda/item-stats", response_model=ItemStatsResponse)
def eda_item_stats(
    item_code: str = Query(..., description="Item code to compute rolling stats for"),
    route_code: Optional[str] = Query(default=None, description="Optional route filter"),
    svc: EdaService = Depends(get_eda_service),
):
    """Rolling averages (last week / 4 weeks / 3 months / 6 months) for a given item."""
    return svc.get_item_stats(item_code, route_code)


