"""
FastAPI dependency injection.
"""

from __future__ import annotations

from functools import lru_cache

from sales_supervision.config.constants import SupervisionConstants
from sales_supervision.config.settings import get_settings
from sales_supervision.core.session import SessionManager
from sales_supervision.services.db_saver import DbSaver
from sales_supervision.services.live_actuals import LiveActualsClient
from sales_supervision.services.storage.store import SessionStore


@lru_cache(maxsize=1)
def get_constants() -> SupervisionConstants:
    return SupervisionConstants()


@lru_cache(maxsize=1)
def get_session_manager() -> SessionManager:
    return SessionManager(get_constants())


@lru_cache(maxsize=1)
def get_store() -> SessionStore:
    return SessionStore(get_settings())


@lru_cache(maxsize=1)
def get_db_saver() -> DbSaver:
    return DbSaver(get_settings())


@lru_cache(maxsize=1)
def get_live_actuals() -> LiveActualsClient:
    return LiveActualsClient(get_settings())
