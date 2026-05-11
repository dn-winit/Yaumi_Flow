"""
Legacy logger shim. Delegates to :mod:`demand_forecasting_pipeline.observability`.

``log_dir`` is accepted for backwards compatibility but ignored - logs go to
stdout so that container log drivers collect them.
"""

from __future__ import annotations

import logging

from ...observability import configure_logging


def get_logger(name: str, log_dir: str | None = None, level: str = "INFO") -> logging.Logger:
    configure_logging(level=level)
    return logging.getLogger(name)
