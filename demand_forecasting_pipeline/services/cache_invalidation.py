"""Shared cache-invalidation helpers for VanLoadService + ArtifactService.

Lazy-imports inside each function so this module stays cheap to import
(api.dependencies triggers full FastAPI DI wiring).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def invalidate_van_load_cache(reason: str = "") -> None:
    """Drop the in-process VanLoadService cache so the next read repopulates from fresh CSV.

    Without busting, the TTL (~5 min) can serve stale van data after a CSV swap.
    ``reason`` is appended to failure logs.
    """
    try:
        from demand_forecasting_pipeline.api.dependencies import get_van_load_service
        get_van_load_service().invalidate()
    except Exception as exc:
        suffix = f" ({reason})" if reason else ""
        logger.warning("van_load_cache_invalidate_failed%s: %s", suffix, exc)


def invalidate_artifact_cache(reason: str = "") -> None:
    """Drop the ArtifactService cache so the next read serves the freshly-overwritten artifact.

    The cache is mtime+size keyed so atomic-rename swaps refresh automatically;
    this helper is for the "want freshness NOW" path (pipeline completion -> /summary).
    """
    try:
        from demand_forecasting_pipeline.api.dependencies import get_artifact_service
        get_artifact_service().invalidate_cache()
    except Exception as exc:
        suffix = f" ({reason})" if reason else ""
        logger.warning("artifact_cache_invalidate_failed%s: %s", suffix, exc)
