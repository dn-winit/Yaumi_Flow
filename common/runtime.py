"""Cross-service runtime helpers.

Small utilities shared by every service's ``__main__.py`` so the launch
contract stays consistent. Keep this module dependency-free -- anything
heavier belongs in the per-service config layer.
"""

from __future__ import annotations

import os


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
