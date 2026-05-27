"""Bounded DB connection pool (one per DSN) with retry-with-backoff.

ODBC pooling reuses sockets but doesn't bound concurrency; the Python
semaphore here caps in-flight connections so a request burst queues
politely instead of blowing past the SQL Server cap. Also the single
source of truth for retry behaviour and connection lifecycle.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Iterator, Optional, TypeVar

import pyodbc

# Enable ODBC driver-level connection caching globally (idempotent).
pyodbc.pooling = True

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Never-retry errors: bad SQL / bad data won't fix itself by waiting.
# Other pyodbc errors (Operational, Interface) are treated as transient.
FATAL_DB_ERRORS = (
    pyodbc.ProgrammingError,
    pyodbc.DataError,
    pyodbc.IntegrityError,
)


def _is_deadlock(exc: BaseException) -> bool:
    """Detect SQL Server deadlock-victim signals (SQLSTATE 40001 / error
    1205 / "deadlock" in message). Caller retries fast since the winning
    txn has already committed by the time we see this.
    """
    if not isinstance(exc, pyodbc.Error) or not exc.args:
        return False
    state = str(exc.args[0]) if exc.args else ""
    msg = " ".join(str(a) for a in exc.args).lower()
    return state == "40001" or "deadlock" in msg or " 1205 " in f" {msg} "

class DbConnectionPool:
    """Semaphore-bounded connection acquirer for a single DSN. Idle
    reuse is the ODBC driver's job; this bounds concurrency only.
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
        """Acquire a connection (blocks until available). Closed on exit
        so the socket returns to the ODBC pool. Per-cursor query timeout
        is pre-applied; callers can override via ``conn.timeout = N``.
        """
        self._sem.acquire()
        conn: Optional[pyodbc.Connection] = None
        try:
            conn = pyodbc.connect(
                self._conn_str,
                autocommit=self._autocommit,
                timeout=self._connect_timeout,
            )
            # SQL command timeout (distinct from connect_timeout so
            # slow handshake and slow query are bounded independently).
            conn.timeout = self._query_timeout
            yield conn
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception as close_exc:  # pragma: no cover
                    logger.warning("db_pool: conn.close() failed: %s", close_exc)
            self._sem.release()

# Process-wide pool registry keyed by (conn_str, autocommit) so
# transactional and autocommit pools against the same DSN don't collide.
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
    """Return the singleton pool for ``conn_str``+``autocommit``; sizing
    args apply only on first creation so a slow consumer can't raise the
    cap mid-flight."""
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
    deadlock_base_delay: float = 0.05,
    deadlock_attempts: int = 5,
) -> Callable[..., T]:
    """Decorator: retry transient DB errors with exponential backoff.
    Deadlocks (SQLSTATE 40001 / err 1205) retry fast (50ms..800ms, up to 5);
    other transient errors retry slowly (1s/2s/4s, capped at ``attempts``).
    ``FATAL_DB_ERRORS`` are never retried.
    """

    @wraps(fn)
    def _wrapper(*args: Any, **kwargs: Any) -> T:
        last_err: Optional[BaseException] = None
        deadlock_seen = 0
        general_attempt = 0
        while True:
            try:
                return fn(*args, **kwargs)
            except FATAL_DB_ERRORS:
                raise  # bad SQL / bad data: fail fast.

            except Exception as exc:
                last_err = exc
                if _is_deadlock(exc):
                    deadlock_seen += 1
                    if deadlock_seen >= deadlock_attempts:
                        break
                    wait = deadlock_base_delay * (2 ** (deadlock_seen - 1))
                    logger.warning(
                        "db_retry deadlock %d/%d: %s; sleeping %.3fs",
                        deadlock_seen, deadlock_attempts, exc, wait,
                    )
                else:
                    general_attempt += 1
                    if general_attempt >= attempts:
                        break
                    wait = base_delay * (2 ** (general_attempt - 1))
                    logger.warning(
                        "db_retry transient %d/%d: %s; sleeping %.1fs",
                        general_attempt, attempts, exc, wait,
                    )
                time.sleep(wait)
        assert last_err is not None  # for type checker
        raise last_err

    return _wrapper

def reset_pools_for_tests() -> None:
    """Test-only: clear the pool registry."""
    with _POOLS_LOCK:
        _POOLS.clear()
