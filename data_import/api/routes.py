"""API routes for data import."""

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
    """Import a single dataset (incremental or full). Cache is invalidated
    in ``finally`` on any successful import -- UPSERT paths can rewrite
    rows in place without growing the count, so unconditional invalidation
    closes the silent-stale window."""
    result: dict = {}
    try:
        result = importer.import_dataset(
            req.dataset, req.mode, lookback_days=req.lookback_days,
        )
        return ImportResponse(**result)
    finally:
        if result.get("success"):
            eda.invalidate()


@router.post("/import-all", response_model=ImportAllResponse)
def import_all(
    req: ImportAllRequest = ImportAllRequest(),
    importer: DataImporter = Depends(get_importer),
    eda: EdaService = Depends(get_eda_service),
):
    """Import all datasets; same try/finally cache-invalidation as /import."""
    results: dict = {}
    try:
        results = importer.import_all(req.mode, lookback_days=req.lookback_days)
        success = all(r.get("success", False) for r in results.values())
        return ImportAllResponse(success=success, results=results)
    finally:
        if any(r.get("success") for r in results.values()):
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
    # Non-blocking DB probe + file-existence-only status. The full status()
    # (row counts, first/last date) belongs on ``/status``, not ``/health``;
    # /health must answer in <100 ms on cold cache so external healthchecks
    # don't churn the container restart loop.
    db_ok = importer.test_connection()
    info = importer.status_quick()
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
    """EDA over sales_recent.csv: totals, daily trend, top items/routes,
    categories. Slice is inclusive on both ends; zero-activity dates render
    as gaps. FilterBar selections AND together against the same window."""
    return svc.get_sales_overview(
        start_date, end_date, warehouse_codes, route_codes, category_codes, item_codes,
    )


@router.get("/eda/items", response_model=ItemCatalogResponse)
def eda_items(svc: EdaService = Depends(get_eda_service)):
    """Item catalog: item_code, name, category, avg_price, last_price, total_quantity."""
    return svc.get_item_catalog()


@router.get("/eda/last-active-date", response_model=LastActiveDateResponse)
def eda_last_active_date(svc: EdaService = Depends(get_eda_service)):
    """Most recent date in sales_recent.csv; drawers seed defaults from
    this so they never land on an empty weekend / holiday."""
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
    """Four exec KPIs from a single (recs join sales) over rec-covered
    (route, date) cells: SKU coverage, field adoption, recommendation
    revenue, peer-uplift potential. Honours FilterBar + window."""
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
    """Cascading filter options for the dashboard FilterBar. Each downstream
    dimension is computed against upstream selections; ``trimmed_selections``
    drops codes no longer present so FE applies the cleaned vector."""
    return svc.get_filter_dimensions(
        warehouse_codes, route_codes, category_codes, item_codes,
    )


# Live YaumiLive cut-throughs; consumed cross-service by supervision's
# LiveActualsClient. 60s cache absorbs rapid-fire visit clicks.


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
    """Per-item van state for one (route, date): past leftover, allocation,
    total load, sold, returns, current leftover, EOD closing. 60s cache."""
    return svc.get_live_van_composition(route_code, date)


@router.get("/eda/item-stats", response_model=ItemStatsResponse)
def eda_item_stats(
    item_code: str = Query(..., description="Item code to compute rolling stats for"),
    route_code: Optional[str] = Query(default=None, description="Optional route filter"),
    svc: EdaService = Depends(get_eda_service),
):
    """Rolling averages (last week / 4 weeks / 3 months / 6 months) for a given item."""
    return svc.get_item_stats(item_code, route_code)


