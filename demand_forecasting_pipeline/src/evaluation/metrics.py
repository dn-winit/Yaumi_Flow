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
TOLERANCE_BY_CLASS: dict[str, float] = {
    "smooth":       0.10,
    "intermittent": 0.20,
    "erratic":      0.30,
    "lumpy":        0.40,
}
DEFAULT_TOLERANCE = 0.20  # unknown / missing class


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
    use it as ``models.selection_metric`` — minimizing it would push
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


def composite_summary(
    actual: np.ndarray,
    predicted: np.ndarray,
    demand_class: Optional[Sequence[str]] = None,
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
            "tolerance_by_class": dict(TOLERANCE_BY_CLASS),
        }

    a = actual[mask]
    p = predicted[mask]

    if demand_class is None:
        tol = np.full(a.shape, DEFAULT_TOLERANCE, dtype=float)
    else:
        cls_arr = np.asarray(demand_class, dtype=object)
        cls_arr = cls_arr[mask]
        tol = np.array(
            [TOLERANCE_BY_CLASS.get(str(c).strip().lower(), DEFAULT_TOLERANCE) for c in cls_arr],
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
        "tolerance_by_class": dict(TOLERANCE_BY_CLASS),
    }


_FUNCS = {
    "mae":   mae,
    "rmse":  rmse,
    "mape":  mape,
    "smape": smape,
    "bias":  bias,
    "wape":  wape,
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
