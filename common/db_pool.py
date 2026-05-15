"""
Bounded DB connection pool (one per DSN) with retry-with-backoff.

Why a Python-level pool when ODBC already pools internally? Two reasons:

  * ``pyodbc.pooling = True`` lets the ODBC driver reuse the underlying
    socket but does NOT bound concurrency. Without a Python semaphore,
    a burst of API requests can blow past the SQL Server connection
    cap and degrade to error pages instead of queueing politely.
  * Centralising the connect / timeout / retry logic in one place keeps
    every consumer (``DbPusher``, ``AccuracyService``, future readers)
    on the same contract - a single source of truth for retry
    behaviour, error classification, and connection lifecycle.

Usage:
    pool = get_pool(conn_str, max_connections=8, query_timeout=300)
    with pool.acquire() as conn:
        cur = conn.cursor()
        cur.execute("...")
        conn.commit()
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Iterator, Optional, TypeVar

import pyodbc

# Enable ODBC driver-level connection caching globally. Idempotent;
# safe to set multiple times (pyodbc just toggles a module flag).
pyodbc.pooling = True

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Errors that should NEVER be retried -- bad SQL / bad data won't
# become right by waiting. Other pyodbc errors (Operational, Interface)
# are treated as transient.
FATAL_DB_ERRORS = (
    pyodbc.ProgrammingError,
    pyodbc.DataError,
    pyodbc.IntegrityError,
)

class DbConnectionPool:
    """Semaphore-bounded connection acquirer for a single DSN.

    The pool itself does not retain idle connections (that's the ODBC
    driver's job); it bounds the *concurrent* count so a flood of
    callers can't open more sockets than the warehouse will tolerate.
    """

    def __init__(
        self,
        conn_str: str,
        *,
        max_connections: int = 8,
        connect_timeout: int = 30,
        query_timeout: int = 300,
        autocommit: bool = False,
    ) -> None:
        if not conn_str:
            raise ValueError("DbConnectionPool: connection string is empty")
        if max_connections < 1:
            raise ValueError(f"DbConnectionPool: max_connections must be >=1, got {max_connections}")
        self._conn_str = conn_str
        self._max = int(max_connections)
        self._connect_timeout = int(connect_timeout)
        self._query_timeout = int(query_timeout)
        self._autocommit = autocommit
        self._sem = threading.Semaphore(self._max)

    @property
    def max_connections(self) -> int:
        return self._max

    @contextmanager
    def acquire(self) -> Iterator[pyodbc.Connection]:
        """Acquire a connection. Blocks until one is available; the
        connection is closed on context exit so the underlying socket
        returns to the ODBC driver's pool for reuse.

        Per-cursor query timeout is pre-applied. Callers can override
        with ``conn.timeout = N`` for an unusually slow statement.
        """
        self._sem.acquire()
        conn: Optional[pyodbc.Connection] = None
        try:
            conn = pyodbc.connect(
                self._conn_str,
                autocommit=self._autocommit,
                timeout=self._connect_timeout,
            )
            # Cursor timeout (per pyodbc docs: this is the SQL command
            # timeout, not the connect timeout). Distinct knob from
            # connect_timeout above so a slow handshake and a slow
            # query are bounded independently.
            conn.timeout = self._query_timeout
            yield conn
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception as close_exc:  # pragma: no cover
                    logger.warning("db_pool: conn.close() failed: %s", close_exc)
            self._sem.release()

# Process-wide pool registry, keyed by (conn_str, autocommit). Two
# callers using the same DSN+mode share one pool. The key includes
# autocommit so a transactional pool and an autocommit pool against
# the same DSN don't collide.
_POOLS: dict[tuple[str, bool], DbConnectionPool] = {}
_POOLS_LOCK = threading.Lock()

def get_pool(
    conn_str: str,
    *,
    max_connections: int = 8,
    connect_timeout: int = 30,
    query_timeout: int = 300,
    autocommit: bool = False,
) -> DbConnectionPool:
    """Return the (single) pool for ``conn_str``+``autocommit``, creating
    it on first call. Sizing arguments only apply to the first creation;
    later calls return the existing pool unchanged so a slow consumer
    can't accidentally raise the cap mid-flight."""
    key = (conn_str, bool(autocommit))
    with _POOLS_LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            pool = DbConnectionPool(
                conn_str,
                max_connections=max_connections,
                connect_timeout=connect_timeout,
                query_timeout=query_timeout,
                autocommit=autocommit,
            )
            _POOLS[key] = pool
    return pool

def with_db_retry(
    fn: Callable[..., T],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
) -> Callable[..., T]:
    """Decorator: retry transient DB errors with exponential backoff.

    Fatal errors (``FATAL_DB_ERRORS``) propagate immediately. The function's
    first positional arg is unused by the decorator - the wrapped callable
    can have any signature; only its DB exception behaviour is changed.

    Backoff: attempt N waits ``base_delay * (2 ** (N-1))`` seconds before
    the next attempt. With defaults (3 attempts, 1s base): 1s, 2s, 4s
    upper bound.
    """

    @wraps(fn)
    def _wrapper(*args: Any, **kwargs: Any) -> T:
        last_err: Optional[BaseException] = None
        for attempt in range(1, attempts + 1):
            try:
                return fn(*args, **kwargs)
            except FATAL_DB_ERRORS:
                # Fail fast - retrying bad SQL or bad data is wasteful.
                raise
            except Exception as exc:
                last_err = exc
                if attempt >= attempts:
                    break
                wait = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "db_retry: attempt %d/%d failed (%s); sleeping %.1fs",
                    attempt, attempts, exc, wait,
                )
                time.sleep(wait)
        assert last_err is not None  # for type checker
        raise last_err

    return _wrapper

def reset_pools_for_tests() -> None:
    """Clear the pool registry. Test-only -- production code never
    needs to reset pools because connection settings are immutable
    after process boot."""
    with _POOLS_LOCK:
        _POOLS.clear()
