"""
Database connection with retry logic.
Uses cursor-based fetching to avoid pandas SQLAlchemy warnings.

Retry policy (single source of truth -- callers everywhere should rely
on this rather than rolling their own loop):

  * Transient errors (``pyodbc.OperationalError``, ``InterfaceError``)
    are retried up to ``retry_attempts`` with a linear back-off of
    ``retry_delay * attempt`` seconds. These cover network blips,
    timeouts, and connection drops.
  * Programming / data errors (``ProgrammingError``, ``DataError``,
    ``IntegrityError``) fail fast on the first attempt -- retrying a
    bad query just wastes time.
  * Anything else is treated as transient (defensive default), so a
    new pyodbc subclass we haven't classified doesn't break behaviour.
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


# Errors that should NEVER be retried -- the query itself is wrong, no
# amount of waiting will fix it. Keeping this list narrow avoids the
# anti-pattern of hiding transient warehouse issues behind a fail-fast
# branch that should retry.
_FATAL_ERRORS = (pyodbc.ProgrammingError, pyodbc.DataError, pyodbc.IntegrityError)


class DatabaseClient:
    """Production DB client with retry and cursor-based DataFrame construction.

    Supports the Live OLTP database (default) and the AIML results database
    via the ``db`` kwarg on :meth:`execute_query`.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        s = settings or get_settings()
        self._live = s.db
        self._aiml = s.aiml_db

    def _connect(self, db: str = "live", *, live_query: bool = False) -> pyodbc.Connection:
        """Open a connection. ``live_query=True`` uses the short-handshake
        timeout so dashboard tiles fail fast instead of hanging on a slow
        warehouse (12-route audit showed bulk imports never overlap with
        dashboard tiles, so the two timeouts can stay distinct)."""
        cfg = self._aiml if db == "aiml" else self._live
        # ``connection_string`` may accept ``live=True`` for the short
        # handshake variant. Live DB exposes both; AIML doesn't.
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
        """Execute query with retry. ``db`` = ``"live"`` (YaumiLive) or
        ``"aiml"`` (YaumiAIML). ``live_query=True`` applies the short
        cursor timeout for dashboard tiles."""
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
                # No retry on bad SQL / bad data; these never become
                # right by waiting.
                logger.error("Fatal DB error (%s) on %s: %s",
                             type(exc).__name__, db, exc)
                raise
            except Exception as exc:
                last_err = exc
                logger.warning("Query attempt %d/%d failed (%s): %s",
                               attempt, cfg.retry_attempts, db, exc)
                if attempt < cfg.retry_attempts:
                    time.sleep(cfg.retry_delay * attempt)
        # Exhausted retries -- raise the last transient error we saw.
        if last_err is not None:
            raise last_err
        raise RuntimeError(f"DB query failed on {db} with no captured error")

    def test_connection(self, db: str = "live") -> Tuple[bool, str]:
        """Liveness probe. Returns ``(ok, reason)`` so health endpoints
        can distinguish "DB unreachable" from "credentials wrong"."""
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
