import numpy as np
from .base import StatForecaster


def _croston_core(y, alpha=0.4, variant="classic"):
    y = np.asarray(y, dtype=float)
    n = len(y)
    if n == 0 or (y > 0).sum() == 0:
        return 0.0
    z = None  # demand level
    p = None  # interval level
    q = 1  # periods since last demand
    for i in range(n):
        if y[i] > 0:
            if z is None:
                z = y[i]
                p = float(q)
            else:
                z = alpha * y[i] + (1 - alpha) * z
                p = alpha * q + (1 - alpha) * p
            q = 1
        else:
            q += 1
    if z is None or p is None or p == 0:
        return 0.0
    if variant == "sba":
        return float((1 - alpha / 2.0) * z / p)
    return float(z / p)


class CrostonForecaster(StatForecaster):
    name = "croston"

    def _predict_one(self, history):
        alpha = float(self.params.get("alpha", 0.4))
        return _croston_core(history, alpha=alpha, variant="classic")


class CrostonSBAForecaster(StatForecaster):
    name = "croston_sba"

    def _predict_one(self, history):
        alpha = float(self.params.get("alpha", 0.4))
        return _croston_core(history, alpha=alpha, variant="sba")
