"""
ETS (Exponential Smoothing) forecaster.

The model fits ``statsmodels.ExponentialSmoothing`` **once per pair** at
fit time and caches the fitted results object. ``predict`` then calls
``.forecast(n)`` against the cached fit - no refits on the per-prediction
path. This matters because the previous design refitted the full state-space
model on every test row, producing an O(pairs x horizon) blow-up that
dominated the training/inference budget at daily scale.

``seasonal_periods`` should be set by the caller based on data granularity
(``data.granularity``); a sensible default is injected in the training
pipeline's ``_inject_granularity_aware_defaults`` helper.
"""

from __future__ import annotations

import warnings

import numpy as np

from .base import StatForecaster

try:
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    _HAS_SM = True
except ImportError:  # pragma: no cover - declared as a dep
    _HAS_SM = False


class ETSForecaster(StatForecaster):
    name = "ets"

    # -- fit ---------------------------------------------------------------

    def fit(self, train_df, group_keys, date_col, target_col, feature_cols):
        super().fit(train_df, group_keys, date_col, target_col, feature_cols)
        self.fit_cache_: dict = {}
        for keys, hist in self.history_.items():
            self.fit_cache_[keys] = self._try_fit_ets(hist)
        return self

    def _try_fit_ets(self, history: np.ndarray):
        if not _HAS_SM or len(history) < 4:
            return None
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                trend = self.params.get("trend", None)
                seasonal = self.params.get("seasonal", None)
                sp = int(self.params.get("seasonal_periods", 7))
                # ``damped_trend`` lets routing dispatch declining pairs to an
                # ETS that doesn't extrapolate the slope linearly forever
                # (which would otherwise predict negative demand). Only valid
                # when ``trend`` is set; statsmodels requires the pair.
                damped = bool(self.params.get("damped_trend", False))
                if seasonal and len(history) < 2 * sp:
                    seasonal = None  # not enough history for the requested seasonality
                model = ExponentialSmoothing(
                    history,
                    trend=trend,
                    seasonal=seasonal,
                    seasonal_periods=sp if seasonal else None,
                    damped_trend=damped if trend else False,
                    initialization_method="estimated",
                )
                return model.fit(optimized=True)
        except Exception:
            return None

    # -- predict -----------------------------------------------------------

    def _predict_pair(self, history: np.ndarray, n: int) -> np.ndarray:
        """Use the cached fit if available; otherwise fall back to the series
        mean. Called once per pair by the base class's ``predict``."""
        # Match the cache entry by history identity - cached state lives at
        # ``self.fit_cache_[pair_key]`` and the base's predict passes the same
        # ``history`` object it stored at fit time.
        fit = self._lookup_fit(history)
        if fit is None:
            fallback = float(np.mean(history)) if len(history) else 0.0
            return np.full(max(n, 1), fallback, dtype=float)
        try:
            return np.asarray(fit.forecast(n), dtype=float)
        except Exception:
            fallback = float(np.mean(history)) if len(history) else 0.0
            return np.full(max(n, 1), fallback, dtype=float)

    def _lookup_fit(self, history: np.ndarray):
        """Locate the pre-fit for this pair.

        Uses identity (``is``) as the fast path -- the base class today
        passes the same array object it stored at fit time, so identity
        matches in O(1) per pair on the happy path. Falls back to
        content equality (shape + ``np.array_equal``) so any future
        ``.copy()`` / ``.astype(float)`` upstream doesn't silently break
        the cache and let every pair fall back to the series mean (which
        would falsely make the metrics report 'ETS competitive').

        ``equal_nan=True`` is essential -- demand panels frequently
        carry NaN for leading-edge rows (no prior observation), and
        ``np.array_equal`` defaults to ``nan != nan`` which would
        silently miss legitimate matches and degrade ETS to the mean
        fallback for every pair with any NaN in its history."""
        if not getattr(self, "fit_cache_", None):
            return None
        # Fast path -- identity match
        for keys, hist in self.history_.items():
            if hist is history:
                return self.fit_cache_.get(keys)
        # Slow path -- content match (handles upstream copies); NaN
        # treated as equal so NaN-bearing histories match.
        for keys, hist in self.history_.items():
            if (
                hist.shape == history.shape
                and np.array_equal(hist, history, equal_nan=True)
            ):
                return self.fit_cache_.get(keys)
        return None

    def _predict_one(self, history):
        # Retained for interface compatibility. Not used on the hot path -
        # fit_cache_ avoids refits.
        fit = self._try_fit_ets(history)
        if fit is None:
            return float(np.mean(history)) if len(history) else 0.0
        try:
            return float(fit.forecast(1)[0])
        except Exception:
            return float(np.mean(history)) if len(history) else 0.0
