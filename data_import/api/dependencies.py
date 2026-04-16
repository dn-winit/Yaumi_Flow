"""
FastAPI dependency injection.
"""

from __future__ import annotations

from functools import lru_cache

from data_import.config.settings import get_settings
from data_import.core.importer import DataImporter
from data_import.services.eda_service import EdaService


@lru_cache(maxsize=1)
def get_importer() -> DataImporter:
    return DataImporter(get_settings())


@lru_cache(maxsize=1)
def get_eda_service() -> EdaService:
    return EdaService(get_settings())
