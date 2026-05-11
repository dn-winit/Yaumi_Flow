from .explainability import router as explainability_router
from .health import router as health_router
from .metrics import router as metrics_router
from .page_views import router as page_views_router
from .pipeline import router as pipeline_router
from .predictions import router as predictions_router
from .reconciliation import router as reconciliation_router
from .retrain import router as retrain_router
from .summary import router as summary_router

__all__ = [
    "explainability_router",
    "health_router",
    "metrics_router",
    "page_views_router",
    "pipeline_router",
    "predictions_router",
    "reconciliation_router",
    "retrain_router",
    "summary_router",
]
