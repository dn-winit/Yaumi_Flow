"""Config loading, directory bootstrap, and dtype resolution.

``resolve_dtypes`` is the single source of truth for CSV column dtypes used
everywhere in the pipeline (training loader, inference loader, artifact
readers, DB pushers). Every caller should pass ``resolve_dtypes(cfg)`` to
``pd.read_csv(dtype=...)`` so RouteCode / ItemCode (or any other
numeric-looking group key) round-trips through CSV as a string — no silent
leading-zero loss, no int↔str merge mismatches.
"""

from __future__ import annotations

import os

import yaml


def load_config(path: str) -> dict:
    """Parse the YAML config at ``path`` into a plain dict."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def ensure_dirs(cfg: dict) -> None:
    """Create every ``paths.*_dir`` entry on disk so downstream writers
    don't have to check for existence on every save."""
    for key, val in (cfg.get("paths") or {}).items():
        if key.endswith("_dir") and val:
            os.makedirs(val, exist_ok=True)


def resolve_dtypes(cfg: dict) -> dict[str, str]:
    """Canonical CSV dtype map — derived from config, applied everywhere.

    Defaults every ``data.forecast_level`` key to ``"string"`` so numeric
    codes survive CSV round-trips intact. Any explicit ``data.dtypes`` entry
    in the YAML overrides that default — letting users who *want* a numeric
    key (``StoreID: int64``) declare it directly.
    """
    data = cfg.get("data") or {}
    group_keys = data.get("forecast_level") or []
    user_overrides = data.get("dtypes") or {}
    return {**{k: "string" for k in group_keys}, **user_overrides}
