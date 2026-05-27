"""Database connection with retry logic (cursor-based to avoid the pandas
SQLAlchemy warning). Transient pyodbc errors retry up to ``retry_attempts``
with linear backoff; ProgrammingError/DataError/IntegrityError fail fast.
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


# Never-retry: query itself is wrong. List kept narrow to avoid masking
# transient warehouse issues behind a fail-fast branch.
_FATAL_ERRORS = (pyodbc.ProgrammingError, pyodbc.DataError, pyodbc.IntegrityError)


class DatabaseClient:
    """DB client with retry + cursor-based DataFrame build. Selects Live
    OLTP (default) or AIML results DB via the ``db`` kwarg."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        s = settings or get_settings()
        self._live = s.db
        self._aiml = s.aiml_db

    def _connect(self, db: str = "live", *, live_query: bool = False) -> pyodbc.Connection:
        """Open a connection; ``live_query=True`` uses the short-handshake
        timeout so dashboard tiles fail fast instead of hanging."""
        cfg = self._aiml if db == "aiml" else self._live
        # Live DB exposes the short-handshake variant; AIML doesn't.
        try:
            cs = cfg.connection_string(live=live_query)  # type: ignore[call-arg]
        except TypeError:
            cs = cfg.connection_string()
        return pyodbc.connect(cs, autocommit=False)

    def execute_query(
        self,
        sql: str,
        params: Tuple = (),
        db: str = "live",
        *,
        live_query: bool = False,
    ) -> pd.DataFrame:
        """Execute query with retry. ``db`` selects YaumiLive/YaumiAIML;
        ``live_query=True`` applies the short cursor timeout."""
        cfg = self._aiml if db == "aiml" else self._live
        last_err: Optional[BaseException] = None
        for attempt in range(1, cfg.retry_attempts + 1):
            try:
                conn = self._connect(db, live_query=live_query)
                try:
                    if live_query and hasattr(cfg, "live_query_timeout"):
                        conn.timeout = int(cfg.live_query_timeout)
                    cursor = conn.cursor()
                    cursor.execute(sql, params)
                    columns = [desc[0] for desc in cursor.description]
                    rows = cursor.fetchall()
                    return pd.DataFrame.from_records(rows, columns=columns)
                finally:
                    conn.close()
            except _FATAL_ERRORS as exc:
                # No retry on bad SQL / bad data.
                logger.error("Fatal DB error (%s) on %s: %s",
                             type(exc).__name__, db, exc)
                raise
            except Exception as exc:
                last_err = exc
                logger.warning("Query attempt %d/%d failed (%s): %s",
                               attempt, cfg.retry_attempts, db, exc)
                if attempt < cfg.retry_attempts:
                    time.sleep(cfg.retry_delay * attempt)
        # Exhausted retries.
        if last_err is not None:
            raise last_err
        raise RuntimeError(f"DB query failed on {db} with no captured error")

    def test_connection(self, db: str = "live") -> Tuple[bool, str]:
        """Liveness probe -- returns ``(ok, reason)`` so health endpoints
        can distinguish unreachable from credentials-wrong."""
        try:
            conn = self._connect(db, live_query=True)
            conn.cursor().execute("SELECT 1")
            conn.close()
            return True, "ok"
        except pyodbc.InterfaceError as exc:
            logger.error("DB credentials invalid (%s): %s", db, exc)
            return False, f"credentials_error: {exc}"
        except pyodbc.OperationalError as exc:
            logger.error("DB unreachable (%s): %s", db, exc)
            return False, f"unreachable: {exc}"
        except Exception as exc:
            logger.error("DB test_connection failed (%s): %s", db, exc)
            return False, f"error: {exc}"
