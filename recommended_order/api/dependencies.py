"""
FastAPI dependency injection -- singleton instances shared across requests.
"""

from __future__ import annotations

from functools import lru_cache

from recommended_order.config.constants import RecommendationConstants
from recommended_order.config.settings import get_settings
from recommended_order.core.engine import RecommendationEngine
from recommended_order.data.manager import DataManager
from recommended_order.services.adoption_service import AdoptionService
from recommended_order.services.db_pusher import DbPusher
from recommended_order.services.planning_service import PlanningService
from recommended_order.services.storage.store import RecommendationStore


@lru_cache(maxsize=1)
def get_constants() -> RecommendationConstants:
    return RecommendationConstants()


@lru_cache(maxsize=1)
def get_data_manager() -> DataManager:
    return DataManager(get_settings())


@lru_cache(maxsize=1)
def get_store() -> RecommendationStore:
    return RecommendationStore(get_settings())


@lru_cache(maxsize=1)
def get_engine() -> RecommendationEngine:
    return RecommendationEngine(get_constants())


@lru_cache(maxsize=1)
def get_db_pusher() -> DbPusher:
    return DbPusher(get_settings())


@lru_cache(maxsize=1)
def get_adoption_service() -> AdoptionService:
    return AdoptionService(get_store(), get_data_manager())


@lru_cache(maxsize=1)
def get_planning_service() -> PlanningService:
    return PlanningService(get_data_manager(), get_settings())
