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
    name (error metrics -> minimize; score metrics -> maximize).
  - Temporal cross-validation uses a **window** per fold, not a single
    date; window size is config-driven or derived from the data when
    absent.
  - The sampler is seeded from ``project.random_seed`` so best-params are
    reproducible for the same data.
  - When tuning cannot run (no Optuna, empty validation data, etc.) the
    reason is logged and an empty param dict is returned - callers fall
    back to ``model_defaults``.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

try:
    import optuna
    from optuna.pruners import MedianPruner
    from optuna.samplers import TPESampler
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    _HAS_OPTUNA = True
except ImportError:  # pragma: no cover - declared as a dep
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
# config - defaults fill in the rest.
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
        # ``seasonal_periods`` is derived from data granularity - not tuned.
        # Only the trend/seasonal structure is searched.
        "trend":            {"type": "categorical", "choices": [None, "add"]},
        "seasonal":         {"type": "categorical", "choices": [None, "add"]},
    },
    # --- two-stage (Prob(demand) classifier x Qty|demand regressor) ---
    # The classifier threshold is the meaningful tunable here. Range
    # spans from "favour recall on small/sparse demand" (0.2) up to
    # "demand to fire" (0.7). Inner regressor params are tuned in
    # their own pass via ``class_tuned_params`` injection upstream.
    "two_stage": {
        "threshold":        {"type": "float", "low": 0.2, "high": 0.7},
    },
}


def _adapt_search_space(
    spec: dict[str, dict],
    *,
    n_train_rows: int,
    divisor: float = 50.0,
    cap_low_floor: int = 100,
    cap_high_ceiling: int = 2000,
) -> dict[str, dict]:
    """Tighten or widen search-space ranges based on training-set size.

    Currently the only adaptation is on tree-ensemble ``n_estimators``:
    the upper bound is set to ``clip(n_train_rows / divisor,
    cap_low_floor, cap_high_ceiling)`` so a small class doesn't waste
    trials on giant ensembles and a big class isn't capped at the
    YAML default. ``divisor``, ``cap_low_floor`` and ``cap_high_ceiling``
    are config-overridable (``hyperparameter_tuning.adaptive_n_estimators``).

    Returns a fresh dict; the input ``spec`` is not mutated.
    """
    out = {k: dict(v) for k, v in spec.items()}
    if "n_estimators" in out and isinstance(out["n_estimators"], dict):
        cap_high = int(np.clip(n_train_rows / max(1e-9, float(divisor)),
                               int(cap_low_floor), int(cap_high_ceiling)))
        cap_low = int(min(out["n_estimators"].get("low", int(cap_low_floor)), cap_high))
        out["n_estimators"]["low"] = cap_low
        out["n_estimators"]["high"] = max(cap_high, cap_low + 1)
    return out


def _resolve_direction(metric: str, configured: str | None) -> str:
    """Returns 'minimize' or 'maximize'. 'auto' (or None) -> infer from metric."""
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

    Returns ``{}`` (empty params -> caller uses model_defaults) when tuning
    cannot meaningfully run; reasons are logged so silent fallbacks don't
    go unnoticed.
    """
    if not _HAS_OPTUNA:
        logger.warning("Optuna not installed - skipping HP tuning for %s", model_name)
        return {}

    tuning_cfg = tuning_cfg or {}
    space_spec = _resolve_search_space(model_name, tuning_cfg.get("search_spaces"))
    if not space_spec:
        logger.warning(
            "No search space configured or defaulted for '%s' - using model defaults",
            model_name,
        )
        return {}

    # Data-size-aware adaptation. The default ranges in
    # ``_DEFAULT_SEARCH_SPACES`` are calibrated for ~5-50k rows; on much
    # larger data the upper bound is the limiting factor for accuracy,
    # on much smaller data the lower bound wastes trials. Recompute
    # bounds adaptively for tree models so search effort lands where it
    # matters.
    # Adaptive bound: any model whose search space declares
    # ``n_estimators`` benefits from data-size-aware capping. Reading
    # the model list from the search-space dict (instead of a hardcoded
    # tuple) keeps the behaviour correct when a future model is added
    # to the spec via config without touching this file.
    adaptive_cfg = (tuning_cfg.get("adaptive_n_estimators") or {})
    has_n_estimators = (
        "n_estimators" in space_spec
        and isinstance(space_spec.get("n_estimators"), dict)
    )
    if has_n_estimators:
        space_spec = _adapt_search_space(
            space_spec,
            n_train_rows=int(len(train_df)),
            divisor=float(adaptive_cfg.get("divisor", 50.0)),
            cap_low_floor=int(adaptive_cfg.get("cap_low_floor", 100)),
            cap_high_ceiling=int(adaptive_cfg.get("cap_high_ceiling", 2000)),
        )

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
    # MedianPruner stops trials whose intermediate score lags the running
    # median by more than ``n_warmup_steps`` epochs. For TPE this typically
    # cuts wall-clock 30-50% with no measurable impact on best_value, since
    # the pruned trials weren't going to win anyway. Models that don't
    # report intermediate values (most of ours, since we evaluate on a
    # held-out slice once per trial) are unaffected -- the pruner is only
    # active if the objective calls trial.report().
    pruner = MedianPruner(n_warmup_steps=1, n_startup_trials=5)
    study = optuna.create_study(direction=direction, sampler=sampler, pruner=pruner)
    try:
        study.optimize(objective, n_trials=n_trials, timeout=timeout, show_progress_bar=False)
    except Exception as exc:
        logger.warning("Optuna study failed for %s: %s", model_name, exc)
        return {}

    # Guard: if every trial failed, ``best_params`` still exists but was never
    # evaluated against a real score. Detect and fall back.
    if study.best_value == fail_score:
        logger.warning(
            "All trials failed for %s (best_value=%s) - returning empty params",
            model_name, study.best_value,
        )
        return {}

    return study.best_params
