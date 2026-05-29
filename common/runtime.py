"""Cross-service runtime helpers.

Small utilities shared by every service's ``__main__.py`` so the launch
contract stays consistent. Keep this module dependency-free -- anything
heavier belongs in the per-service config layer.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

# Tokens that mean "operator hasn't actually filled this in" -- the literal
# strings shipped in ``.env.example`` plus the common variants developers
# use as scratch placeholders. ``configured()`` checks below treat any DB
# host / username matching these as "not configured" so cron jobs cleanly
# skip the write step instead of crashing on ``pyodbc.connect``.
_PLACEHOLDER_TOKENS: frozenset[str] = frozenset({
    "your_username", "your-username",
    "your_password", "your-password",
    "your-db-server", "your_db_server",
    "your_api_key", "your-api-key",
    "your_database", "your-database",
    "change_me", "changeme",
    "placeholder", "set_me", "__set_me__",
    "todo", "tbd",
})


def is_placeholder_value(value: str | None) -> bool:
    """True iff ``value`` is one of the recognised placeholder tokens.

    Lowercase + stripped before compare so ``Your-DB-Server`` and
    ``  your_username  `` both register as placeholders. Empty / None
    is reported as ``True`` (treat unset as placeholder).
    """
    if value is None:
        return True
    s = value.strip().lower()
    if not s:
        return True
    # Anything starting with ``your_`` or ``your-`` is shorthand-placeholder.
    if s.startswith("your_") or s.startswith("your-"):
        return True
    return s in _PLACEHOLDER_TOKENS


def is_production() -> bool:
    """True only when ``YF_ENV=production``. Used by the boot-time env
    validation below: dev/staging keeps the soft-skip behaviour where
    missing optional URLs degrade silently; production hard-fails so an
    operator can't deploy a half-configured service.
    """
    return os.getenv("YF_ENV", "").strip().lower() == "production"


def require_env(names: Iterable[str], *, service: str) -> None:
    """In production, hard-fail boot when any of ``names`` is unset or
    empty. No-op in dev/staging so a developer running the stack
    locally without a full warehouse config still gets working services
    (the soft-skip paths in cascade helpers handle the missing URL
    cleanly).

    Centralised here so every service applies the same prod-vs-dev
    invariant -- otherwise each ``__main__.py`` would re-implement the
    YF_ENV check with subtle drift.
    """
    if not is_production():
        return
    missing = [n for n in names if not (os.getenv(n) or "").strip()]
    if missing:
        raise RuntimeError(
            f"{service}: required environment variables unset in production: "
            f"{sorted(missing)}. Set YF_ENV to anything other than 'production' "
            f"to allow the soft-skip behaviour, or populate these env vars "
            f"before booting."
        )


def port_from_env(default: int, var_name: str = "PORT") -> int:
    """Return the port the service should bind to.

    ``PORT`` (when set) wins over the configured default -- Cloud Run,
    Heroku, Railway, and Fly all assign one dynamically at deploy time
    and expect the app to honour it. Locally and in docker-compose the
    env var is unset, so each service falls back to its canonical port
    (e.g. 8001 for recommended_order, 8005 for data_import) baked into
    its ``Settings.port`` default.

    Raises ``ValueError`` if ``PORT`` is set but not a positive integer
    in the legal port range -- noisy boot beats a silent bind on an
    unexpected interface.
    """
    raw = os.getenv(var_name)
    if not raw:
        return default
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError(f"{var_name}={raw!r} is not a valid integer") from exc
    if not (1 <= port <= 65535):
        raise ValueError(f"{var_name}={port} is outside the legal port range 1-65535")
    return port
