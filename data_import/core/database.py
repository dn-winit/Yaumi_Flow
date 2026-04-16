"""
Database connection with retry logic.
Uses cursor-based fetching to avoid pandas SQLAlchemy warnings.
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Tuple

import pandas as pd
import pyodbc

from data_import.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

pyodbc.pooling = True


class DatabaseClient:
    """Production DB client with retry and cursor-based DataFrame construction.

    Supports the Live OLTP database (default) and the AIML results database
    via the ``db`` kwarg on :meth:`execute_query`.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        s = settings or get_settings()
        self._live = s.db
        self._aiml = s.aiml_db

    def _connect(self, db: str = "live") -> pyodbc.Connection:
        cfg = self._aiml if db == "aiml" else self._live
        return pyodbc.connect(cfg.connection_string(), autocommit=False)

    def execute_query(self, sql: str, params: Tuple = (), db: str = "live") -> pd.DataFrame:
        """Execute query with retry. ``db`` = ``"live"`` (YaumiLive) or ``"aiml"`` (YaumiAIML)."""
        cfg = self._aiml if db == "aiml" else self._live
        last_err = None
        for attempt in range(1, cfg.retry_attempts + 1):
            try:
                conn = self._connect(db)
                try:
                    cursor = conn.cursor()
                    cursor.execute(sql, params)
                    columns = [desc[0] for desc in cursor.description]
                    rows = cursor.fetchall()
                    return pd.DataFrame.from_records(rows, columns=columns)
                finally:
                    conn.close()
            except Exception as exc:
                last_err = exc
                logger.warning("Query attempt %d/%d failed (%s): %s", attempt, cfg.retry_attempts, db, exc)
                if attempt < cfg.retry_attempts:
                    time.sleep(cfg.retry_delay * attempt)
        raise last_err  # type: ignore

    def test_connection(self, db: str = "live") -> bool:
        try:
            conn = self._connect(db)
            conn.cursor().execute("SELECT 1")
            conn.close()
            return True
        except Exception as exc:
            logger.error("Connection test failed (%s): %s", db, exc)
            return False
