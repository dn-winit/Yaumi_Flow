"""Single-leader election for in-process schedulers via OS file lock.

With uvicorn --workers N, the FastAPI lifespan starts schedulers per worker;
without coordination, crons fire N times in parallel against the same DB tables.
Filesystem-scoped advisory lock (POSIX flock / Windows msvcrt.locking); kernel
releases on process exit. Multi-host needs a distributed lease layered on top.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

# Module-level singleton; lock handle survives async lifespan for the worker's lifetime.
_LEASE_LOCK = threading.Lock()
_LEASE_FH: object | None = None
_LEASE_PATH: Path | None = None


def _try_lock_posix(fh) -> bool:
    """Non-blocking exclusive flock on POSIX. Returns True if acquired."""
    import fcntl
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _try_lock_windows(fh) -> bool:
    """Non-blocking exclusive lock via msvcrt.locking on a 1-byte region."""
    import msvcrt
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    except OSError:
        return False


def _pid_path(lock_path: Path) -> Path:
    """Sibling .pid file holding leader's pid; split out so Windows msvcrt mandatory lock
    doesn't break ops reads on the lock file."""
    return lock_path.with_suffix(lock_path.suffix + ".pid")


def try_acquire_leader_lock(lock_path: str | Path) -> bool:
    """True = this worker is now leader; False = another holds the lease.

    Held in module-level singleton for the worker's lifetime; OS releases on exit/crash.
    """
    global _LEASE_FH, _LEASE_PATH
    with _LEASE_LOCK:
        if _LEASE_FH is not None:
            # Already the leader in this process. Idempotent.
            return True

        # Operator opt-out for single-worker deployments / tests where the
        # filesystem lock is genuinely unavailable; explicit env beats a
        # silent fail-open.
        if os.environ.get("YF_LEADER_LOCK_DISABLE", "").strip().lower() in {"1", "true", "yes"}:
            logger.warning(
                "leader_election: YF_LEADER_LOCK_DISABLE set; treating this "
                "worker as leader without acquiring a file lock. ONLY safe "
                "under workers=1.",
            )
            return True

        path = Path(lock_path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            # Can't even create the lock dir -- every worker hitting this fails
            # closed (follower) so we never have N workers all claiming leadership
            # on a misconfigured shared mount. Operator must fix the path.
            logger.error(
                "leader_election: cannot create lock dir %s (%s); "
                "running as follower (schedulers disabled). Fix the lock "
                "path or set YF_LEADER_LOCK_DISABLE=1 to opt out.",
                path.parent, exc,
            )
            return False
        # Append-mode for create-if-missing; never write -- pid lives in sibling .pid.
        try:
            fh = open(path, "a+", encoding="utf-8")
        except OSError as exc:
            # Fail closed: a permission/EROFS failure is a deployment error,
            # not a license to fire N copies of every cron.
            logger.error(
                "leader_election: cannot open %s (%s); running as follower "
                "(schedulers disabled). Fix the lock path or set "
                "YF_LEADER_LOCK_DISABLE=1 to opt out.",
                path, exc,
            )
            return False

        is_windows = sys.platform.startswith("win")
        acquired = _try_lock_windows(fh) if is_windows else _try_lock_posix(fh)
        if not acquired:
            fh.close()
            logger.info(
                "leader_election: another worker holds %s; this worker "
                "will run as a follower (schedulers disabled)",
                path,
            )
            return False

        # Pid in sibling file (lock file's byte 0 refuses reads on Windows).
        try:
            _pid_path(path).write_text(f"{os.getpid()}\n", encoding="utf-8")
        except OSError:
            # Non-fatal -- the lock itself is what enforces leadership.
            pass

        _LEASE_FH = fh
        _LEASE_PATH = path
        logger.info(
            "leader_election: acquired %s (pid=%d); this worker is the "
            "scheduler leader",
            path, os.getpid(),
        )
        return True


def is_leader() -> bool:
    """True iff this process currently holds the leader lease."""
    return _LEASE_FH is not None


def release_leader_lock() -> None:
    """Explicit release + close. Normal path relies on process exit; this is for tests
    and explicit shutdown hooks."""
    global _LEASE_FH, _LEASE_PATH
    with _LEASE_LOCK:
        fh = _LEASE_FH
        if fh is None:
            return
        is_windows = sys.platform.startswith("win")
        if is_windows:
            try:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            fh.close()
        except OSError:
            pass
        # Remove sibling .pid so next leader doesn't inherit stale pid.
        if _LEASE_PATH is not None:
            try:
                _pid_path(_LEASE_PATH).unlink(missing_ok=True)
            except OSError:
                pass
        _LEASE_FH = None
        _LEASE_PATH = None
