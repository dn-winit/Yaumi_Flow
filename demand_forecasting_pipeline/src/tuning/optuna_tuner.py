"""
Hyperparameter tuning via Optuna.

Design
------
Everything external to the objective is config-driven:

  - Search bounds come from ``cfg.search_spaces.<model>``; the in-code
    ``_DEFAULT_SEARCH_SPACES`` below is used only when a model is missing
    from config. That preserves "no hardcoding" without forcing users to
    re-type every bound.
  - ``direction`` is either taken from config or derived from the metric
    name (error metrics → minimize; score metrics → maximize).
  - Temporal cross-validation uses a **window** per fold, not a single
    date; window size is config-driven or derived from the data when
    absent.
  - The sampler is seeded from ``project.random_seed`` so best-params are
    reproducible for the same data.
  - When tuning cannot run (no Optuna, empty validation data, etc.) the
    reason is logged and an empty param dict is returned — callers fall
    back to ``model_defaults``.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

try:
    import optuna
    from optuna.samplers import TPESampler
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    _HAS_OPTUNA = True
except ImportError:  # pragma: no cover — declared as a dep
    _HAS_OPTUNA = False

from ..evaluation.metrics import compute_all
from ..models.registry import build_model

logger = logging.getLogger(__name__)


# Metrics where higher is better. Everything else is assumed to be an error
# metric (lower is better). Keep this list in sync with evaluation.metrics.
_MAXIMIZE_METRICS = frozenset({
    "r2", "accuracy", "accuracy_pct", "precision", "recall", "f1",
})


# Fallback search spaces when a model has no entry in
# ``cfg.hyperparameter_tuning.search_spaces``. Each entry is a mapping of
# parameter name -> {type, low, high, [log]}. Users override any subset via
# config — defaults fill in the rest.
_DEFAULT_SEARCH_SPACES: dict[str, dict[str, dict]] = {
    # --- ML / tree models ---
    "lightgbm": {
        "n_estimators":     {"type": "int",   "low": 100, "high": 400},
        "learning_rate":    {"type": "float", "low": 0.01, "high": 0.2, "log": True},
        "num_leaves":       {"type": "int",   "low": 15,  "high": 127},
        "min_data_in_leaf": {"type": "int",   "low": 5,   "high": 40},
        "feature_fraction": {"type": "float", "low": 0.6, "high": 1.0},
        "bagging_fraction": {"type": "float", "low": 0.6, "high": 1.0},
    },
    "xgboost": {
        "n_estimators":     {"type": "int",   "low": 100, "high": 400},
        "learning_rate":    {"type": "float", "low": 0.01, "high": 0.2, "log": True},
        "max_depth":        {"type": "int",   "low": 3,   "high": 10},
        "subsample":        {"type": "float", "low": 0.6, "high": 1.0},
        "colsample_bytree": {"type": "float", "low": 0.6, "high": 1.0},
    },
    "random_forest": {
        "n_estimators":     {"type": "int",   "low": 100, "high": 300},
        "max_depth":        {"type": "int",   "low": 4,   "high": 14},
        "min_samples_leaf": {"type": "int",   "low": 2,   "high": 10},
    },
    # --- linear / regularised ---
    "linear": {
        "alpha":            {"type": "float", "low": 0.01, "high": 10.0, "log": True},
    },
    # --- classical / statistical (small search; tuning converges fast) ---
    "moving_average": {
        "window":           {"type": "int",   "low": 2,   "high": 28},
    },
    "croston": {
        "alpha":            {"type": "float", "low": 0.05, "high": 0.5},
    },
    "croston_sba": {
        "alpha":            {"type": "float", "low": 0.05, "high": 0.5},
    },
    "ets": {
        # ``seasonal_periods`` is derived from data granularity — not tuned.
        # Only the trend/seasonal structure is searched.
        "trend":            {"type": "categorical", "choices": [None, "add"]},
        "seasonal":         {"type": "categorical", "choices": [None, "add"]},
    },
}


def _resolve_direction(metric: str, configured: str | None) -> str:
    """Returns 'minimize' or 'maximize'. 'auto' (or None) → infer from metric."""
    if configured and configured not in ("auto", "", None):
        if configured not in ("minimize", "maximize"):
            raise ValueError(
                f"hyperparameter_tuning.direction must be 'auto', 'minimize', "
                f"or 'maximize'; got {configured!r}"
            )
        return configured
    return "maximize" if (metric or "").lower() in _MAXIMIZE_METRICS else "minimize"


def _resolve_search_space(model_name: str, cfg_spaces: dict | None) -> dict[str, dict]:
    """Config override wins; missing entries filled from defaults."""
    defaults = _DEFAULT_SEARCH_SPACES.get(model_name, {})
    user = (cfg_spaces or {}).get(model_name) or {}
    merged = dict(defaults)
    merged.update(user)
    return merged


def _suggest(trial, name: str, spec: dict):
    """Call the right ``trial.suggest_*`` for a single search-space entry."""
    kind = (spec.get("type") or "float").lower()
    low, high = spec["low"], spec["high"]
    if kind == "int":
        return trial.suggest_int(name, int(low), int(high))
    if kind == "float":
        return trial.suggest_float(name, float(low), float(high), log=bool(spec.get("log", False)))
    if kind == "categorical":
        return trial.suggest_categorical(name, list(spec["choices"]))
    raise ValueError(f"Unknown search-space type {kind!r} for parameter {name!r}")


def _temporal_cv_splits(df: pd.DataFrame, date_col: str, n_splits: int, val_size: int | None):
    """Walk-forward CV with a validation WINDOW per fold (not a single date).

    ``val_size`` is the number of distinct dates each fold's validation window
    spans. When ``None`` or non-positive, defaults to ``max(1, n // (n_splits + 1))``
    so the folds tile the tail of the data without overlap.
    """
    dates = sorted(df[date_col].unique())
    n = len(dates)
    if n < n_splits + 2:
        return [(df, df.iloc[0:0])]

    if not val_size or val_size <= 0:
        val_size = max(1, n // (n_splits + 1))

    splits = []
    for i in range(n_splits):
        # Folds are anchored at the tail of the date range. The i-th fold ends
        # ``(n_splits - 1 - i) * val_size`` dates before the end.
        val_end = n - (n_splits - 1 - i) * val_size
        val_start = val_end - val_size
        if val_start < 1:
            continue
        val_dates = set(dates[val_start:val_end])
        train_dates = set(dates[:val_start])
        tr = df[df[date_col].isin(train_dates)]
        va = df[df[date_col].isin(val_dates)]
        if not tr.empty and not va.empty:
            splits.append((tr, va))
    return splits or [(df, df.iloc[0:0])]


def tune_model(
    model_name: str,
    train_df: pd.DataFrame,
    val_df: pd.DataFrame,
    group_keys: list[str],
    date_col: str,
    target_col: str,
    feature_cols: list[str],
    *,
    n_trials: int,
    timeout: int,
    metric: str,
    tuning_cfg: dict | None = None,
    random_seed: int | None = None,
) -> dict:
    """Run an Optuna study and return the winning params.

    Returns ``{}`` (empty params → caller uses model_defaults) when tuning
    cannot meaningfully run; reasons are logged so silent fallbacks don't
    go unnoticed.
    """
    if not _HAS_OPTUNA:
        logger.warning("Optuna not installed — skipping HP tuning for %s", model_name)
        return {}

    tuning_cfg = tuning_cfg or {}
    space_spec = _resolve_search_space(model_name, tuning_cfg.get("search_spaces"))
    if not space_spec:
        logger.warning(
            "No search space configured or defaulted for '%s' — using model defaults",
            model_name,
        )
        return {}

    direction = _resolve_direction(metric, tuning_cfg.get("direction"))
    tcv = tuning_cfg.get("temporal_cv") or {}
    use_temporal_cv = bool(tcv.get("enabled", False)) and not train_df.empty

    # Feasibility: either temporal CV on train+val, or holdout on val_df.
    if not use_temporal_cv and (val_df is None or val_df.empty):
        logger.warning(
            "Skipping HP tuning for %s: validation set is empty and temporal_cv disabled",
            model_name,
        )
        return {}

    fail_score = float("inf") if direction == "minimize" else float("-inf")

    def _score(actual, predicted) -> float:
        out = compute_all(actual, predicted, [metric])
        v = out.get(metric)
        return fail_score if (v is None or not np.isfinite(v)) else float(v)

    def objective(trial):
        params = {name: _suggest(trial, name, spec) for name, spec in space_spec.items()}
        try:
            if use_temporal_cv:
                full = (
                    pd.concat([train_df, val_df], ignore_index=True)
                    if not val_df.empty else train_df
                )
                splits = _temporal_cv_splits(
                    full, date_col,
                    n_splits=int(tcv.get("n_splits", 3)),
                    val_size=tcv.get("val_size"),
                )
                scores = []
                for tr, va in splits:
                    if va.empty:
                        continue
                    mdl = build_model(model_name, params)
                    mdl.fit(tr, group_keys, date_col, target_col, feature_cols)
                    preds = mdl.predict(va, group_keys, date_col, target_col, feature_cols)
                    merged = va[[target_col]].reset_index(drop=True)
                    merged["prediction"] = preds["prediction"].values
                    scores.append(_score(merged[target_col].values, merged["prediction"].values))
                return float(np.mean(scores)) if scores else fail_score
            else:
                mdl = build_model(model_name, params)
                mdl.fit(train_df, group_keys, date_col, target_col, feature_cols)
                preds = mdl.predict(val_df, group_keys, date_col, target_col, feature_cols)
                merged = val_df[group_keys + [date_col, target_col]].merge(
                    preds, on=group_keys + [date_col], how="left",
                )
                merged["prediction"] = merged["prediction"].fillna(0.0)
                return _score(merged[target_col].values, merged["prediction"].values)
        except Exception as exc:
            logger.debug("Trial failed for %s: %s", model_name, exc)
            return fail_score

    sampler = TPESampler(seed=random_seed) if random_seed is not None else TPESampler()
    study = optuna.create_study(direction=direction, sampler=sampler)
    try:
        study.optimize(objective, n_trials=n_trials, timeout=timeout, show_progress_bar=False)
    except Exception as exc:
        logger.warning("Optuna study failed for %s: %s", model_name, exc)
        return {}

    # Guard: if every trial failed, ``best_params`` still exists but was never
    # evaluated against a real score. Detect and fall back.
    if study.best_value == fail_score:
        logger.warning(
            "All trials failed for %s (best_value=%s) — returning empty params",
            model_name, study.best_value,
        )
        return {}

    return study.best_params
