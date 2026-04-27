"""
API routes for data import.
"""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, Query

from data_import.api.dependencies import get_eda_service, get_importer
from data_import.api.schemas import (
    DataSummaryResponse,
    DatasetInfo,
    HealthResponse,
    ImportAllRequest,
    ImportAllResponse,
    ImportRequest,
    ImportResponse,
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
    """Import a single dataset (incremental or full)."""
    result = importer.import_dataset(req.dataset, req.mode)
    if result.get("success") and result.get("new_rows", 0) > 0:
        eda.invalidate()  # CSV changed -> aggregations are now stale
    return ImportResponse(**result)


@router.post("/import-all", response_model=ImportAllResponse)
def import_all(
    req: ImportAllRequest = ImportAllRequest(),
    importer: DataImporter = Depends(get_importer),
    eda: EdaService = Depends(get_eda_service),
):
    """Import all datasets."""
    results = importer.import_all(req.mode)
    success = all(r.get("success", False) for r in results.values())
    if any(r.get("new_rows", 0) > 0 for r in results.values()):
        eda.invalidate()
    return ImportAllResponse(success=success, results=results)


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


@router.get("/eda/sales")
def eda_sales(
    lookback: str = Query(
        default="last_7_working_days",
        description="Reporting period: last_working_day | last_7_working_days",
    ),
    warehouse_codes: List[str] = Query(default=[], alias="warehouse_codes"),
    route_codes: List[str] = Query(default=[], alias="route_codes"),
    category_codes: List[str] = Query(default=[], alias="category_codes"),
    item_codes: List[str] = Query(default=[], alias="item_codes"),
    svc: EdaService = Depends(get_eda_service),
):
    """Aggregated EDA over sales_recent.csv: totals, daily trend, top items, top routes, categories.

    The `lookback` enum slices the data to the trailing N **working days**
    (active dates with sales), so weekends / holidays / closures are
    excluded automatically. FilterBar selections are honoured together
    against the same windowed slice.
    """
    return svc.get_sales_overview(
        lookback, warehouse_codes, route_codes, category_codes, item_codes,
    )


@router.get("/eda/items")
def eda_items(svc: EdaService = Depends(get_eda_service)):
    """Item catalog: item_code, name, category, avg_price, last_price, total_quantity."""
    return svc.get_item_catalog()


@router.get("/eda/business-kpis")
def eda_business_kpis(
    lookback: str = Query(
        default="last_7_working_days",
        description="Reporting period: last_working_day | last_7_working_days",
    ),
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

    All four respect the FilterBar + the active reporting period, and are
    evaluated only over (route, date) cells that actually have rec coverage.
    """
    return svc.get_business_kpis(
        lookback, warehouse_codes, route_codes, category_codes, item_codes,
    )


@router.get("/eda/filter-dimensions")
def eda_filter_dimensions(
    warehouse_codes: List[str] = Query(default=[], alias="warehouse_codes"),
    route_codes: List[str] = Query(default=[], alias="route_codes"),
    category_codes: List[str] = Query(default=[], alias="category_codes"),
    svc: EdaService = Depends(get_eda_service),
):
    """Cascading filter options for the dashboard FilterBar.

    Each downstream dimension is the set of unique values present in the
    sales slice that already matches every upstream selection. Items are
    filtered by all three upstream levels.
    """
    return svc.get_filter_dimensions(warehouse_codes, route_codes, category_codes)


# ------------------------------------------------------------------
# Live YaumiLive cut-throughs. Consumed cross-service by the supervision
# microservice (LiveActualsClient) so the supervisor's "Mark visited"
# flow can pull real actuals without a direct DB grant. 60-second cache
# absorbs rapid-fire visit clicks on the same (route, date) cell.
# ------------------------------------------------------------------


@router.get("/eda/live-route-sales")
def eda_live_route_sales(
    route_code: str = Query(..., description="Route code"),
    date: str = Query(..., description="YYYY-MM-DD"),
    svc: EdaService = Depends(get_eda_service),
):
    return svc.get_live_route_sales(route_code, date)


@router.get("/eda/live-customer-sales")
def eda_live_customer_sales(
    route_code: str = Query(..., description="Route code"),
    date: str = Query(..., description="YYYY-MM-DD"),
    customer_code: str = Query(..., description="Customer code"),
    svc: EdaService = Depends(get_eda_service),
):
    return svc.get_live_customer_sales(route_code, date, customer_code)


@router.get("/eda/item-stats")
def eda_item_stats(
    item_code: str = Query(..., description="Item code to compute rolling stats for"),
    route_code: Optional[str] = Query(default=None, description="Optional route filter"),
    svc: EdaService = Depends(get_eda_service),
):
    """Rolling averages (last week / 4 weeks / 3 months / 6 months) for a given item."""
    return svc.get_item_stats(item_code, route_code)


@router.get("/eda/forecast-rows")
def eda_forecast_rows(
    lookback: str = Query(
        default="last_7_working_days",
        description="Reporting period: last_working_day | last_7_working_days",
    ),
    warehouse_codes: List[str] = Query(default=[], alias="warehouse_codes"),
    route_codes: List[str] = Query(default=[], alias="route_codes"),
    category_codes: List[str] = Query(default=[], alias="category_codes"),
    item_codes: List[str] = Query(default=[], alias="item_codes"),
    svc: EdaService = Depends(get_eda_service),
):
    """Per-(date, route, item) forecast vs actual rows for the VanLoad
    Past-analysis drawer. Same merge as /eda/business-kpis -- one shared
    compute path so the drawer numbers always reconcile with the dashboard.
    """
    return svc.get_forecast_rows(
        lookback, warehouse_codes, route_codes, category_codes, item_codes,
    )


