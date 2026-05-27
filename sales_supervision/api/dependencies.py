"""FastAPI DI; all deps are lru_cache singletons shared with the auto-visit reconciler."""

from __future__ import annotations

from functools import lru_cache

from sales_supervision.config.constants import SupervisionConstants
from sales_supervision.config.settings import get_settings
from sales_supervision.core.session import SessionManager
from sales_supervision.services.db_saver import DbSaver
from sales_supervision.services.live_actuals import LiveActualsClient
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
def get_auto_visit_service():
    """Compose the reconciler from cached singletons; lazy import to keep DI import cheap.

    LlmClient is no longer wired in -- LLM analyses are generated on-demand
    by the webapp calling llm_analytics directly; the cron path doesn't fire
    or persist any LLM output.
    """
    from sales_supervision.services.auto_visit_service import AutoVisitService
    return AutoVisitService(
        settings=get_settings(),
        session_manager=get_session_manager(),
        live_actuals=get_live_actuals(),
        recommended_order=get_recommended_order_client(),
        db_saver=get_db_saver(),
    )
