import numpy as np
from .base import StatForecaster


class MovingAverageForecaster(StatForecaster):
    name = "moving_average"

    def _predict_one(self, history):
        w = int(self.params.get("window", 3))
        if len(history) == 0:
            return 0.0
        h = history[-w:]
        return float(np.mean(h))
