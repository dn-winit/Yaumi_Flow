"""
API routes for data import.
"""

from __future__ import annotations

from typing import Optional

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
def eda_sales(svc: EdaService = Depends(get_eda_service)):
    """Aggregated EDA over sales_recent.csv: totals, daily trend, top items, top routes, categories."""
    return svc.get_sales_overview()


@router.get("/eda/items")
def eda_items(svc: EdaService = Depends(get_eda_service)):
    """Item catalog: item_code, name, category, avg_price, last_price, total_quantity."""
    return svc.get_item_catalog()


@router.get("/eda/business-kpis")
def eda_business_kpis(svc: EdaService = Depends(get_eda_service)):
    """Actionable daily-ops KPIs: yesterday revenue, 7d trend, accuracy, today ops."""
    return svc.get_business_kpis()


@router.get("/eda/live-route-sales")
def eda_live_route_sales(
    route_code: str = Query(..., description="Route code"),
    date: str = Query(..., description="YYYY-MM-DD"),
    svc: EdaService = Depends(get_eda_service),
):
    """All customers who invoiced on a route/date from YaumiLive. 60-s cache."""
    return svc.get_live_route_sales(route_code, date)


@router.get("/eda/live-customer-sales")
def eda_live_customer_sales(
    route_code: str = Query(..., description="Route code"),
    date: str = Query(..., description="YYYY-MM-DD"),
    customer_code: str = Query(..., description="Customer code"),
    svc: EdaService = Depends(get_eda_service),
):
    """Live per-item sales for a single (route, date, customer), pulled from
    YaumiLive on demand. 60-second TTL cache."""
    return svc.get_live_customer_sales(route_code, date, customer_code)


@router.get("/eda/item-stats")
def eda_item_stats(
    item_code: str = Query(..., description="Item code to compute rolling stats for"),
    route_code: Optional[str] = Query(default=None, description="Optional route filter"),
    svc: EdaService = Depends(get_eda_service),
):
    """Rolling averages (last week / 4 weeks / 3 months / 6 months) for a given item."""
    return svc.get_item_stats(item_code, route_code)


@router.get("/eda/customers")
def eda_customers(
    lookback_days: int = Query(default=90, ge=7, le=730),
    svc: EdaService = Depends(get_eda_service),
):
    """Live customer overview from YaumiLive: active customers, top customers, by-route breakdown."""
    return svc.get_customer_overview(lookback_days)


@router.post("/eda/refresh")
def eda_refresh(svc: EdaService = Depends(get_eda_service)):
    """Force-refresh cached EDA aggregates."""
    svc.invalidate()
    return {"success": True, "message": "EDA cache cleared"}
