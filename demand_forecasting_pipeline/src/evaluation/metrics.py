"""
Forecast error metrics.

Every function returns a plain float (or ``None`` when the input has no
scorable rows), so callers can store results in JSON/CSV without further
conversion. ``compute_all`` is the single dispatch point used by the
training and evaluation code paths.
"""

from __future__ import annotations

from typing import Iterable

import numpy as np

_EPS = 1e-9


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


def wape_summary(actual: np.ndarray, predicted: np.ndarray) -> dict:
    """WAPE-based accuracy plus the underlying sums, so callers can build
    richer response objects without re-deriving the math.

    Returns::
        {
          "wape":            float,  # percent
          "accuracy_pct":    float,  # max(0, 100 - wape)
          "rows_compared":   int,    # scored-subset size
          "total_predicted": float,  # sum over ALL rows (business total)
          "total_actual":    float,  # sum over ALL rows (business total)
          "scored_actual":   float,  # sum over scored subset (WAPE denom)
          "scored_abs_err":  float,  # sum |actual - pred| over scored subset
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
        }
    scored_actual = float(actual[mask].sum())
    scored_abs_err = float(np.abs(actual[mask] - predicted[mask]).sum())
    wape_pct = scored_abs_err / scored_actual * 100.0 if scored_actual > 0 else 0.0
    return {
        "wape": round(wape_pct, 2),
        "accuracy_pct": round(max(0.0, 100.0 - wape_pct), 2),
        "rows_compared": int(mask.sum()),
        "total_predicted": total_predicted,
        "total_actual": total_actual,
        "scored_actual": scored_actual,
        "scored_abs_err": scored_abs_err,
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
