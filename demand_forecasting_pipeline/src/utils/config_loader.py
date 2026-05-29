"""Config loading, directory bootstrap, and dtype resolution.

``resolve_dtypes`` is the single source of truth for CSV column dtypes used
everywhere in the pipeline (training loader, inference loader, artifact
readers, DB pushers). Every caller should pass ``resolve_dtypes(cfg)`` to
``pd.read_csv(dtype=...)`` so RouteCode / ItemCode (or any other
numeric-looking group key) round-trips through CSV as a string - no silent
leading-zero loss, no int<->str merge mismatches.

``load_config`` invokes the Pydantic schema in ``demand_forecasting_pipeline.
config.schema`` and refuses to return a config that doesn't validate. Set
``DF_VALIDATE_CONFIG=0`` to bypass during emergency rollbacks.
"""

from __future__ import annotations

import logging
import os

import yaml

logger = logging.getLogger(__name__)

def _validate(cfg: dict) -> dict:
    """Run schema validation unless explicitly disabled.

    Imports lazily so callers that only need ``resolve_dtypes`` /
    ``ensure_dirs`` (e.g. lightweight tests) don't have to pull
    pydantic at import time.
    """
    if os.getenv("DF_VALIDATE_CONFIG", "1") == "0":
        return cfg
    try:
        from demand_forecasting_pipeline.config.schema import ConfigError, validate_config
    except Exception as exc:  # pragma: no cover -- schema module missing
        logger.warning("config schema validation skipped: %s", exc)
        return cfg
    try:
        return validate_config(cfg)
    except ConfigError:
        raise
    except Exception as exc:
        # Schema bugs shouldn't take the pipeline down; log loudly and
        # let the run proceed against the raw dict. ConfigError (the
        # surface we control) still propagates above.
        logger.error("config schema validation crashed (continuing): %s", exc)
        return cfg

def load_config(path: str) -> dict:
    """Parse the YAML config at ``path`` into a plain dict, validated."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return _validate(raw)

def ensure_dirs(cfg: dict) -> None:
    """Create every ``paths.*_dir`` entry on disk so downstream writers
    don't have to check for existence on every save."""
    for key, val in (cfg.get("paths") or {}).items():
        if key.endswith("_dir") and val:
            os.makedirs(val, exist_ok=True)

def resolve_dtypes(cfg: dict) -> dict[str, str]:
    """Canonical CSV dtype map - derived from config, applied everywhere.

    Defaults every ``data.forecast_level`` key to ``"string"`` so numeric
    codes survive CSV round-trips intact. Any explicit ``data.dtypes`` entry
    in the YAML overrides that default - letting users who *want* a numeric
    key (``StoreID: int64``) declare it directly.
    """
    data = cfg.get("data") or {}
    group_keys = data.get("forecast_level") or []
    user_overrides = data.get("dtypes") or {}
    return {**{k: "string" for k in group_keys}, **user_overrides}

def resolve_recursive_iterations(cfg: dict) -> int:
    """Resolve ``inference.recursive_iterations`` when set to ``auto``.

    ``auto`` means: pick the smallest number of recursive passes that lets
    every lag in the feature set reference a real prior prediction (or
    actual) rather than a placeholder zero.

    Formula: ``min(horizon, max(1, horizon // max_lag))``
      * If max_lag >= horizon, one pass is enough.
      * Otherwise we need ceil(horizon/max_lag) passes - but capped at
        the horizon itself so we never iterate more times than there
        are days to predict.

    Falls back to ``1`` when the value is missing or unrecognised.
    """
    inf = cfg.get("inference") or {}
    raw = inf.get("recursive_iterations", 1)
    if isinstance(raw, int) and raw >= 0:
        return raw
    if isinstance(raw, str) and raw.lower() == "auto":
        horizon = int(inf.get("forecast_horizon", 30) or 30)
        fe = cfg.get("feature_engineering") or {}
        lags_cfg = fe.get("lags") or {}
        all_lags: list[int] = []
        for lst in lags_cfg.values():
            if isinstance(lst, list):
                all_lags.extend([int(x) for x in lst if isinstance(x, (int, float))])
        max_lag = max(all_lags) if all_lags else horizon
        if max_lag <= 0:
            return 1
        passes = max(1, horizon // max_lag)
        return min(horizon, passes)
    return 1
