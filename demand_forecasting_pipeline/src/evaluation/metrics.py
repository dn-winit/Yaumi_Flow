import numpy as np


def mae(y_true, y_pred):
    return float(np.mean(np.abs(y_true - y_pred)))


def rmse(y_true, y_pred):
    return float(np.sqrt(np.mean((y_true - y_pred) ** 2)))


def mape(y_true, y_pred, eps=1e-9):
    mask = np.abs(y_true) > eps
    if mask.sum() == 0:
        return None
    return float(np.mean(np.abs((y_true[mask] - y_pred[mask]) / y_true[mask])) * 100.0)


def smape(y_true, y_pred, eps=1e-9):
    denom = (np.abs(y_true) + np.abs(y_pred)) / 2.0 + eps
    return float(np.mean(np.abs(y_true - y_pred) / denom) * 100.0)


def bias(y_true, y_pred):
    return float(np.mean(y_pred - y_true))


def wape(y_true, y_pred, eps=1e-9):
    # Score only rows where actual > 0 AND predicted > 0. Consistent with all
    # runtime accuracy computations (summary.py, accuracy_service.py,
    # retrain_scheduler.py, eda_service.py, AccuracyDrawer.tsx). Zero-actual
    # rows produce undefined percentage errors; zero-predicted rows mean the
    # item wasn't recommended, so they're not part of recommendation accuracy.
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    mask = (y_true > eps) & (y_pred > eps)
    if not mask.any():
        return None
    s = float(np.sum(y_true[mask]))
    return float(np.sum(np.abs(y_true[mask] - y_pred[mask])) / s * 100.0)


def wape_summary(actual, predicted):
    """Canonical accuracy summary used by every display site.

    Scores only rows where both actual > 0 and predicted > 0, then returns
    the WAPE-based accuracy plus the underlying sums so callers can build
    richer response objects without re-deriving the math.

    Returns::
        {
          "wape":            float,  # percent, 0..100+
          "accuracy_pct":    float,  # max(0, 100 - wape)
          "rows_compared":   int,    # size of the scored subset
          "total_predicted": float,  # sum over ALL input rows (business total)
          "total_actual":    float,  # sum over ALL input rows (business total)
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
    scored_actual_arr = actual[mask]
    scored_pred_arr = predicted[mask]
    scored_actual = float(scored_actual_arr.sum())
    scored_abs_err = float(np.abs(scored_actual_arr - scored_pred_arr).sum())
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


_FUNCS = {"mae": mae, "rmse": rmse, "mape": mape, "smape": smape, "bias": bias, "wape": wape}


def compute_all(y_true, y_pred, names):
    y_true = np.asarray(y_true, dtype=float)
    y_pred = np.asarray(y_pred, dtype=float)
    out = {}
    for n in names:
        f = _FUNCS.get(n)
        if f is None:
            continue
        try:
            out[n] = f(y_true, y_pred)
        except Exception:
            out[n] = None
    return out
