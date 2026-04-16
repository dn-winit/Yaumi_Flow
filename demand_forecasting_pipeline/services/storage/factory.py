"""
Storage factory -- returns file-based storage backend.
The StorageBackend interface allows adding new backends (e.g. database) later.
"""

from __future__ import annotations

from typing import Optional

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.services.storage.file_storage import FileStorage


def create_storage(settings: Optional[Settings] = None) -> FileStorage:
    return FileStorage(settings or get_settings())
