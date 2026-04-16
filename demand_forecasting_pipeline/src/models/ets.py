import warnings
import numpy as np
from .base import StatForecaster

try:
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    _HAS_SM = True
except Exception:
    _HAS_SM = False


class ETSForecaster(StatForecaster):
    name = "ets"

    def _predict_one(self, history):
        if not _HAS_SM or len(history) < 4:
            return float(np.mean(history)) if len(history) else 0.0
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                trend = self.params.get("trend", None)
                seasonal = self.params.get("seasonal", None)
                sp = int(self.params.get("seasonal_periods", 7))
                if seasonal and len(history) < 2 * sp:
                    seasonal = None
                model = ExponentialSmoothing(
                    history, trend=trend, seasonal=seasonal,
                    seasonal_periods=sp if seasonal else None,
                    initialization_method="estimated",
                )
                fit = model.fit(optimized=True)
                return float(fit.forecast(1)[0])
        except Exception:
            return float(np.mean(history)) if len(history) else 0.0
