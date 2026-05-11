"""FastAPI dependency injection.

All dependencies are LRU-cached to a singleton -- one bias service,
one DB saver, one HTTP client per upstream service. The auto-visit
reconciler reuses the same singletons the request-driven routes do,
so a config change picked up by one path is picked up by both.
"""

from __future__ import annotations

from functools import lru_cache

from sales_supervision.config.constants import SupervisionConstants
from sales_supervision.config.settings import get_settings
from sales_supervision.core.session import SessionManager
from sales_supervision.services.db_saver import DbSaver
from sales_supervision.services.live_actuals import LiveActualsClient
from sales_supervision.services.llm_client import LlmClient
from sales_supervision.services.recommended_order_client import RecommendedOrderClient


@lru_cache(maxsize=1)
def get_constants() -> SupervisionConstants:
    return SupervisionConstants()


@lru_cache(maxsize=1)
def get_session_manager() -> SessionManager:
    return SessionManager(get_constants())


@lru_cache(maxsize=1)
def get_db_saver() -> DbSaver:
    return DbSaver(get_settings())


@lru_cache(maxsize=1)
def get_live_actuals() -> LiveActualsClient:
    return LiveActualsClient(get_settings())


@lru_cache(maxsize=1)
def get_recommended_order_client() -> RecommendedOrderClient:
    return RecommendedOrderClient(get_settings())


@lru_cache(maxsize=1)
def get_llm_client() -> LlmClient:
    return LlmClient(get_settings())


@lru_cache(maxsize=1)
def get_auto_visit_service():
    """Compose the reconciler from the cached singletons. Imported
    lazily because the service drags in apscheduler/httpx; keeping the
    import at module top-level would slow ``from
    sales_supervision.api.dependencies import get_db_saver`` style calls."""
    from sales_supervision.services.auto_visit_service import AutoVisitService
    return AutoVisitService(
        settings=get_settings(),
        session_manager=get_session_manager(),
        live_actuals=get_live_actuals(),
        recommended_order=get_recommended_order_client(),
        db_saver=get_db_saver(),
        llm_client=get_llm_client(),
    )
