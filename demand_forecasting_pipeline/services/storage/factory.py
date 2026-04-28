"""
Storage factory -- always returns the local filesystem backend.

The codebase used to ship an S3 alternative for cloud deployments; we
removed it because the integrated monorepo writes artifacts beside the
service on the same host. Keep this module as the single construction
seam in case a remote backend is reintroduced.
"""

from __future__ import annotations

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.services.storage.base import StorageBackend
from demand_forecasting_pipeline.services.storage.file_storage import FileStorage


def create_storage(settings: Settings | None = None) -> StorageBackend:
    return FileStorage(settings or get_settings())
