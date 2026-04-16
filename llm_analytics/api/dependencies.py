"""
FastAPI dependency injection.
"""

from __future__ import annotations

from functools import lru_cache

from llm_analytics.config.settings import get_settings
from llm_analytics.core.analyzer import Analyzer


@lru_cache(maxsize=1)
def get_analyzer() -> Analyzer:
    return Analyzer(get_settings())
