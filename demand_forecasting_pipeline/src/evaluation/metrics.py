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
    s = float(np.sum(np.abs(y_true)))
    if s < eps:
        return None
    return float(np.sum(np.abs(y_true - y_pred)) / s * 100.0)


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
