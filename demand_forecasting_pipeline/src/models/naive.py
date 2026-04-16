
from .base import StatForecaster


class NaiveForecaster(StatForecaster):
    name = "naive"

    def _predict_one(self, history):
        if len(history) == 0:
            return 0.0
        return float(history[-1])
