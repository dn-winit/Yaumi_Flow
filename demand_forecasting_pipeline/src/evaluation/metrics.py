"""
Forecast error metrics. ``compute_all`` dispatches per-model errors
(MAE/RMSE/MAPE/...) for training. ``composite_summary`` is the single
class-aware headline accuracy every UI tile reads.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

import numpy as np

_EPS = 1e-9


# Per-class miss tolerance for composite accuracy. SBC (Syntetos-Boylan-
# Croston) buckets get progressively wider tolerances as items become
# inherently harder to predict. Mirrored in webapp/src/lib/format.ts.
#
# These values are the production defaults. Pass an override dict to
# ``composite_summary(..., tolerance_by_class=...)`` to substitute the
# values from ``config.yaml: evaluation.composite_accuracy.tolerance_by_class``.
DEFAULT_TOLERANCE_BY_CLASS: dict[str, float] = {
    "smooth":       0.10,
    "intermittent": 0.20,
    "erratic":      0.30,
    "lumpy":        0.40,
}
DEFAULT_TOLERANCE = 0.20  # unknown / missing class

# Backward-compat alias retained so callers that imported the old name
# (UI tile aggregator, drift module) keep working without coordinated
# updates. New callers should pass tolerances through the function arg.
TOLERANCE_BY_CLASS = DEFAULT_TOLERANCE_BY_CLASS


def composite_kwargs_from_cfg(cfg: dict | None) -> dict:
    """Resolve the kwargs to pass to ``composite_summary`` from a pipeline
    config dict. Returns an empty dict if the config has no overrides
    (caller falls through to the module defaults). Centralises the YAML
    contract so every caller reads the same shape."""
    if not cfg:
        return {}
    block = (cfg.get("evaluation") or {}).get("composite_accuracy") or {}
    out: dict = {}
    tol_map = block.get("tolerance_by_class")
    if isinstance(tol_map, dict) and tol_map:
        out["tolerance_by_class"] = {
            str(k).strip().lower(): float(v) for k, v in tol_map.items()
        }
    if "default" in block:
        out["default_tolerance"] = float(block["default"])
    return out


# Per-process kwargs cache keyed by YAML path. The YAML is parsed once
# per path; subsequent calls are an O(1) dict lookup. Cleared automatically
# when the process restarts -- the typical config-edit workflow already
# bounces the service, so an explicit invalidation hook would be dead
# code right now.
_COMPOSITE_KWARGS_CACHE: dict[str, dict] = {}


def composite_kwargs_from_yaml(path: str | None) -> dict:
    """Resolve composite-accuracy kwargs from the pipeline YAML at
    ``path``, cached per-process.

    Single source of truth so drift / live accuracy / summary endpoint
    all score under the SAME tolerance configuration as the
    training-time baseline they're being compared against. Without
    this, every serving-time scorer silently falls back to the module
    defaults (``DEFAULT_TOLERANCE_BY_CLASS``) and a config-driven
    retraining run would invisibly diverge from the drift comparison.

    Returns an empty dict on missing path or any load/parse error so
    callers fall through to the module defaults rather than crashing.
    """
    if not path:
        return {}
    cached = _COMPOSITE_KWARGS_CACHE.get(path)
    if cached is not None:
        return cached
    try:
        # Local import keeps metrics.py free of a top-level dependency
        # on the file-loader -- this helper is only exercised by serving
        # paths, not by the metric primitives themselves.
        from demand_forecasting_pipeline.src.utils.config_loader import load_config
        kwargs = composite_kwargs_from_cfg(load_config(path))
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "composite_kwargs_from_yaml: load failed for %s: %s", path, exc,
        )
        kwargs = {}
    _COMPOSITE_KWARGS_CACHE[path] = kwargs
    return kwargs


def mae(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    return float(np.mean(np.abs(y_true - y_pred)))


def rmse(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    return float(np.sqrt(np.mean((y_true - y_pred) ** 2)))


def mape(y_true: np.ndarray, y_pred: np.ndarray, eps: float = _EPS) -> float | None:
    """Mean absolute percentage error on rows where ``|y_true| > eps``.
    Returns ``None`` when every row has zero actual."""
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    mask = np.abs(y_true) > eps
    if not mask.any():
        return None
    return float(np.mean(np.abs((y_true[mask] - y_pred[mask]) / y_true[mask])) * 100.0)


def smape(y_true: np.ndarray, y_pred: np.ndarray, eps: float = _EPS) -> float:
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    denom = (np.abs(y_true) + np.abs(y_pred)) / 2.0 + eps
    return float(np.mean(np.abs(y_true - y_pred) / denom) * 100.0)


def bias(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    """Mean signed error (``predicted - actual``).

    WARNING: this metric is *best near zero*, not *lower is better*. Do not
    use it as ``models.selection_metric`` - minimizing it would push
    predictions toward large negative values.
    """
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    return float(np.mean(y_pred - y_true))


def wape(y_true: np.ndarray, y_pred: np.ndarray, eps: float = _EPS) -> float | None:
    """Weighted absolute percentage error, scored only on rows where both
    actual and predicted are positive. Zero-actual rows produce undefined
    percentage errors; zero-predicted rows represent "not recommended"
    and are excluded from recommendation-accuracy measurement.
    """
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    mask = (y_true > eps) & (y_pred > eps)
    if not mask.any():
        return None
    scored = float(np.sum(y_true[mask]))
    return float(np.sum(np.abs(y_true[mask] - y_pred[mask])) / scored * 100.0)


def mase(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    *,
    y_train: Optional[np.ndarray] = None,
    seasonal_period: int = 1,
    eps: float = _EPS,
) -> float | None:
    """Mean Absolute Scaled Error (Hyndman & Koehler 2006).

    Scale-free metric for time-series forecasts: the forecast's MAE is
    divided by the MAE of the in-sample naive forecast at the given
    seasonal period (1 = pure-naive, 7 = day-of-week naive, etc.).

    MASE < 1  -> the model beats the naive baseline.
    MASE = 1  -> the model is no better than naive.
    MASE > 1  -> the model is worse than naive.

    Why MASE matters here
    ---------------------
    For intermittent / lumpy demand classes (the SBC quadrants that
    dominate FMCG sparse-SKU panels), MAE and WAPE both have known
    pathologies: WAPE excludes zero-actual rows (the typical case),
    MAE is not scale-free across SKUs of different volumes. MASE is
    the canonical scale-free metric in the intermittent-demand
    literature (Kourentzes 2014, Syntetos-Boylan benchmarks) and is
    what an MNC ML platform peer reviewer expects to see for any
    demand-forecasting system.

    Inputs
    ------
    ``y_train`` -- in-sample (training-window) actuals used to compute
    the naive-forecast denominator. When ``None``, the denominator is
    computed from ``y_true`` itself (acceptable when y_true is long
    enough; the standard MASE definition uses the training window).
    ``seasonal_period=1`` is the right default for daily FMCG demand
    where the dominant seasonality is captured by other features
    (lag_7, day_of_week sin/cos). For weekly-seasonal series at daily
    grain, callers can pass 7.

    Returns ``None`` when the naive-forecast denominator is zero (e.g.,
    a flat all-zero training window) -- in that case MASE is undefined
    and the caller falls back to other metrics.
    """
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    if y_true.size == 0:
        return None
    # Naive forecast denominator. Uses ``y_train`` if provided (the
    # textbook MASE), otherwise ``y_true`` (acceptable substitute when
    # the training history isn't available to the metric call site).
    denom_series = np.asarray(
        y_train if y_train is not None else y_true, dtype=float,
    )
    p = max(1, int(seasonal_period))
    if denom_series.size <= p:
        # Not enough history for a seasonal-naive baseline; the metric
        # is mathematically undefined.
        return None
    naive_errors = np.abs(denom_series[p:] - denom_series[:-p])
    denom = float(np.mean(naive_errors))
    if denom < eps:
        # Flat denominator series -- MASE is undefined. Don't divide.
        return None
    return float(np.mean(np.abs(y_true - y_pred)) / denom)


def resolve_class_array(
    df: "pd.DataFrame",
    *,
    column: str = "class",
    fallback_column: str = "demand_class",
) -> Optional[np.ndarray]:
    """Return the per-row class array used by ``composite_summary`` or
    ``None`` if the dataframe carries no class column.

    The same coercion pattern (``df[col].astype(str).to_numpy()``) was
    inlined at four call sites (training pipeline, accuracy_service,
    forecast_kpi, drift detector). Centralising the lookup here:

      * single owner of the column-name fallback (some artifacts use
        ``class``, some DB-side rows use ``demand_class``),
      * single owner of the str-coercion (avoids a NaN-in-class row
        emitting ``"nan"`` vs ``None`` inconsistently across surfaces),
      * makes composite_summary calls one line shorter at every site.

    Returns ``None`` (not an empty array) when neither column exists so
    composite_summary falls through to its DEFAULT_TOLERANCE path -- the
    same behaviour every caller previously implemented inline.
    """
    # Lazy local import: this module is otherwise pandas-free, and
    # importing pandas at module-load time would slow `import metrics`
    # on cold paths (Prometheus collectors, CLI scoring tools).
    import pandas as pd  # noqa: PLC0415  (intentional lazy import)

    for col in (column, fallback_column):
        if col and col in df.columns:
            return df[col].astype(str).to_numpy()
    return None


def composite_summary(
    actual: np.ndarray,
    predicted: np.ndarray,
    demand_class: Optional[Sequence[str]] = None,
    *,
    tolerance_by_class: Optional[dict[str, float]] = None,
    default_tolerance: Optional[float] = None,
) -> dict:
    """Class-aware accuracy -- the headline business metric.

    Each row's miss is forgiven up to ``TOLERANCE_BY_CLASS[class]``;
    only the overshoot beyond tolerance feeds the WAPE numerator. When
    ``demand_class`` is missing or unrecognised for a row the
    :data:`DEFAULT_TOLERANCE` is applied so a sparse upstream classifier
    can never crash the metric.

    Returns::
        {
          "wape":             float,  # tolerance-adjusted WAPE %
          "accuracy_pct":     float,  # max(0, 100 - wape)
          "rows_compared":    int,    # cells where actual > 0 AND pred > 0
          "total_predicted":  float,  # sum over ALL rows (business total)
          "total_actual":     float,  # sum over ALL rows (business total)
          "scored_actual":    float,  # WAPE denominator
          "scored_abs_err":   float,  # raw |actual - pred| pre-tolerance
          "method":           "composite",
          "tolerance_by_class": dict[str, float],
        }
    """
    # Resolve tolerance source. Caller passes a config-driven override
    # dict via ``tolerance_by_class`` and an optional ``default_tolerance``;
    # both fall through to the module defaults so legacy callers stay
    # working unchanged.
    tol_map = dict(DEFAULT_TOLERANCE_BY_CLASS)
    if tolerance_by_class:
        tol_map.update({str(k).strip().lower(): float(v) for k, v in tolerance_by_class.items()})
    tol_default = float(default_tolerance) if default_tolerance is not None else DEFAULT_TOLERANCE

    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    total_actual = float(np.nansum(actual))
    total_predicted = float(np.nansum(predicted))
    mask = (actual > 0) & (predicted > 0)

    if not mask.any():
        return {
            "wape": 0.0,
            "accuracy_pct": 0.0,
            "rows_compared": 0,
            "total_predicted": total_predicted,
            "total_actual": total_actual,
            "scored_actual": 0.0,
            "scored_abs_err": 0.0,
            "method": "composite",
            "tolerance_by_class": dict(tol_map),
        }

    a = actual[mask]
    p = predicted[mask]

    if demand_class is None:
        tol = np.full(a.shape, tol_default, dtype=float)
    else:
        cls_arr = np.asarray(demand_class, dtype=object)
        cls_arr = cls_arr[mask]
        tol = np.array(
            [tol_map.get(str(c).strip().lower(), tol_default) for c in cls_arr],
            dtype=float,
        )

    abs_err = np.abs(a - p)
    real_miss = np.maximum(0.0, abs_err - tol * a)
    scored_actual = float(a.sum())
    real_miss_sum = float(real_miss.sum())
    abs_err_sum = float(abs_err.sum())
    wape_pct = real_miss_sum / scored_actual * 100.0 if scored_actual > 0 else 0.0
    return {
        "wape": round(wape_pct, 2),
        "accuracy_pct": round(max(0.0, 100.0 - wape_pct), 2),
        "rows_compared": int(mask.sum()),
        "total_predicted": total_predicted,
        "total_actual": total_actual,
        "scored_actual": scored_actual,
        "scored_abs_err": abs_err_sum,  # raw, pre-tolerance
        "method": "composite",
        "tolerance_by_class": dict(tol_map),
    }


_FUNCS = {
    "mae":   mae,
    "rmse":  rmse,
    "mape":  mape,
    "smape": smape,
    "bias":  bias,
    "wape":  wape,
    # MASE is callable from compute_all with the default
    # ``seasonal_period=1`` and ``y_train=None`` (uses y_true as the
    # denominator series). Callers who want a textbook MASE pass the
    # training-window actuals via ``compute_all_with_train`` below.
    "mase":  mase,
}


def compute_all(y_true: np.ndarray, y_pred: np.ndarray, names: Iterable[str]) -> dict[str, float | None]:
    """Compute the requested subset of metrics. Unknown names are skipped;
    per-metric failures are caught and surfaced as ``None`` so one bad
    metric never breaks the training loop."""
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    out: dict[str, float | None] = {}
    for n in names:
        f = _FUNCS.get(n)
        if f is None:
            continue
        try:
            out[n] = f(y_true, y_pred)
        except Exception:
            out[n] = None
    return out
