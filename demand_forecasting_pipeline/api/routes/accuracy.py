"""
Accuracy comparison endpoint -- predicted (YaumiAIML) vs live actual (YaumiLive).
Cross-DB join with consistent aggregation.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from typing import Optional

from demand_forecasting_pipeline.api.dependencies import get_accuracy_service
from demand_forecasting_pipeline.services.accuracy_service import AccuracyService

router = APIRouter(prefix="/accuracy", tags=["accuracy"])


@router.get("/comparison")
def comparison(
    start_date: Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    end_date: Optional[str] = Query(default=None, description="YYYY-MM-DD"),
    route_code: Optional[str] = Query(default=None),
    item_code: Optional[str] = Query(default=None),
    limit: int = Query(default=2000, ge=1, le=10000),
    svc: AccuracyService = Depends(get_accuracy_service),
):
    """
    Returns predicted vs LIVE actual rows + summary KPIs.

    - Predicted: from YaumiAIML.yf_demand_forecast (our pipeline output)
    - Actual: live from YaumiLive.VW_GET_SALES_DETAILS
    - Aggregation: GROUP BY (TrxDate, RouteCode, ItemCode), SUM positive QuantityInPCs
    """
    return svc.get_comparison(start_date, end_date, route_code, item_code, limit)
