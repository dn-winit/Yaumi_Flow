"""Reconciliation layer: convert raw demand forecast -> recommended van load.

Three modules, each independently testable:

* :class:`BiasService` -- per (route, item) rolling bias multiplier learned
  from past ``Predicted`` vs ``ActualQty`` pairs in ``demand_forecast.csv``.
* :class:`VanLoadService` -- assembles the actual van composition
  (yesterday's leftover + today's allocation + sales/returns) from shared
  CSVs (historical) or via the ``data_import`` HTTP endpoint (live).
* :class:`ReconciliationEngine` -- pure-arithmetic V5_b formula:
    ``load = max(carry_floor * P / (1 + bias),  P / (1 + bias) - opening)``.

The split keeps each module independently auditable per FVA discipline:
toggle bias on/off or carry on/off and re-measure lift.
"""

from demand_forecasting_pipeline.services.reconciliation.bias_service import BiasService
from demand_forecasting_pipeline.services.reconciliation.van_load_service import VanLoadService
from demand_forecasting_pipeline.services.reconciliation.engine import (
    ReconciliationEngine,
    LoadRecommendation,
)
from demand_forecasting_pipeline.services.reconciliation.enrich import enrich_with_load

__all__ = [
    "BiasService",
    "VanLoadService",
    "ReconciliationEngine",
    "LoadRecommendation",
    "enrich_with_load",
]
