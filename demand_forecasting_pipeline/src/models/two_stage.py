"""
Two-stage (hurdle) forecaster.

For demand series with many zeros, modelling the joint distribution of
``y`` as a continuous random variable wastes the model's capacity on
the zero-inflation. We split into two heads instead:

  Stage 1 (classifier)
      P(y > 0 | X) -- probability of any demand at all. Trained on the
      full panel; ``y_bin = 1{y > 0}``.

  Stage 2 (regressor)
      E[y | y > 0, X] -- conditional quantity when demand occurs. Fit
      ONLY on the non-zero training rows so the regressor concentrates
      its capacity on shape-of-positive-demand instead of being pulled
      to zero by the zero majority.

The final prediction is the expected value of demand under the
decomposition::

    E[y | X] = P(y > 0 | X) * E[y | y > 0, X]

This expected-value (EV) form is the standard production deployment of
a hurdle model. The earlier hard-threshold form
``predict = qty_if_demand if p_demand >= 0.5 else 0`` threw away
information at the decision boundary -- a 49% vs 51% probability gave
predictions of 0 vs full-quantity, brittle around the threshold and
dependent on calibrated probabilities at exactly the chosen cutoff,
which the raw ``class_weight="balanced"`` classifier did not provide.

Calibration
-----------
``LogisticRegression(class_weight="balanced")`` learns a good decision
function on imbalanced data but its ``predict_proba`` outputs are NOT
honest probabilities -- the class re-weighting deliberately distorts
the score map (the classifier optimises a reweighted log-loss; the
softmax-of-margins is no longer the posterior). We wrap the
classifier in ``CalibratedClassifierCV`` so the output is a real
probability that can be multiplied by the regressor's quantity
estimate.

Defaults: ``method="isotonic"`` (the standard choice when the base
estimator has enough capacity to overshoot and needs monotonic
correction; sigmoid/Platt is right for low-capacity bases like a raw
two-feature LR but isotonic generalises better when the LR is fed a
many-dimensional feature matrix). Both are configurable.

Adaptive cv
-----------
Calibration is cross-validated. ``StratifiedKFold(n_splits=K)``
requires at least K samples of EACH class. For a stable calibration
fit we want at least 2 samples per fold per class, so the effective
cv is::

    eff_cv = min(requested_cv, n_minority_class // 2)

When ``eff_cv < 2`` we cannot run cross-validated calibration at all.
The fallback is to use the un-calibrated classifier pipeline -- a
warning is logged so the operator can see when calibration was
unavailable and decide whether more training data is needed.

Feature scaling
---------------
``LogisticRegression``'s loss surface is sensitive to feature
magnitudes. We wrap the LR in
``Pipeline(StandardScaler, LogisticRegression)`` so:

  * fit and predict apply identical scaling (the scaler is fit on
    train, the same statistics are used at predict)
  * the scaler state is pickled alongside the LR weights, so a
    deserialised model never sees a train/serve scaling skew
  * fillna(0.0) on the input is consistent across fit and predict

Backward compatibility
----------------------
Pickled artifacts written before this redesign do not carry
``prediction_mode_`` or ``calibrate_classifier_``. ``predict`` falls
back to the OLD behaviour (threshold mode, raw classifier) when those
attributes are missing -- so deploying the new code does NOT silently
change the predictions of already-trained models. Retraining picks up
the new defaults (EV mode + calibration).
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from .base import BaseForecaster
from .lightgbm_model import LightGBMForecaster, _HAS_LGB
from .random_forest import RandomForestForecaster

logger = logging.getLogger(__name__)


# Single source of truth for the valid prediction modes. A typo in the
# YAML surfaces here (not at predict time) because __init__ validates
# against this set.
_VALID_PREDICTION_MODES: tuple[str, ...] = ("expected_value", "threshold")
_DEFAULT_PREDICTION_MODE = "expected_value"

# Valid calibration methods. ``isotonic`` is the default for the
# reasons in the module docstring; ``sigmoid`` is exposed for the
# small-data case where isotonic's piecewise-constant fit can overfit
# the calibration set.
_VALID_CALIBRATION_METHODS: tuple[str, ...] = ("isotonic", "sigmoid")
_DEFAULT_CALIBRATION_METHOD = "isotonic"

# Minimum samples-per-fold-per-class for cross-validated calibration.
# ``StratifiedKFold(n_splits=K)`` requires >= K samples of each class;
# requiring 2 per fold gives the calibrator something to fit on
# instead of pure variance. Translates to ``n_min // 2 >= eff_cv``.
_MIN_SAMPLES_PER_FOLD = 2


class TwoStageForecaster(BaseForecaster):
    """Hurdle model: calibrated classifier + regressor-on-positives.

    Parameters (all read from ``params`` with defaults documented inline):

    Stage 1 (classifier)
      ``max_iter`` (int, default 1000)
          LogisticRegression iteration cap.
      ``calibrate_classifier`` (bool, default True)
          Wrap the LR pipeline in CalibratedClassifierCV so
          ``predict_proba`` outputs honest probabilities. Disabled
          automatically when the minority class is too small for
          cross-validation (see ``calibration_cv``).
      ``calibration_method`` (str, default "isotonic")
          One of ``isotonic`` / ``sigmoid``. Isotonic dominates for
          most demand-forecasting feature matrices; sigmoid is the
          safer pick for very small calibration sets.
      ``calibration_cv`` (int, default 5)
          Upper bound on the cross-validation fold count for
          calibration. The effective fold count adapts down to
          ``n_minority // 2`` so very imbalanced data doesn't crash
          StratifiedKFold.

    Stage 2 (regressor)
      ``regressor`` (str, default "lightgbm")
          One of ``lightgbm`` / ``random_forest``. If lightgbm is
          requested but the optional dep is missing, we fall back to
          random_forest (a hard-dep) so training never fails on
          environment.
      ``regressor_params`` (dict, default {})
          Forwarded verbatim to the chosen regressor.
      ``min_nonzero_for_regressor`` (int, default 5)
          If fewer positive training rows than this, skip the
          regressor and fall back to the unconditional train-window
          mean. Belt-and-braces against degenerate fits.

    Combine
      ``prediction_mode`` (str, default "expected_value")
          One of ``expected_value`` / ``threshold``. EV is the
          mathematically correct hurdle deployment; ``threshold`` is
          kept for backward compatibility with operators who want a
          hard go/no-go cutoff.
      ``threshold`` (float, default 0.5)
          Cutoff for ``threshold`` mode. Ignored in ``expected_value``
          mode.
    """

    name = "two_stage"

    def __init__(self, params: dict | None = None) -> None:
        super().__init__(params)
        # Validate prediction_mode at construct time so a YAML typo
        # surfaces here, before any training cost has been spent.
        mode = str(
            self.params.get("prediction_mode", _DEFAULT_PREDICTION_MODE),
        ).strip().lower()
        if mode not in _VALID_PREDICTION_MODES:
            raise ValueError(
                f"two_stage prediction_mode={mode!r} is invalid; "
                f"expected one of {list(_VALID_PREDICTION_MODES)}"
            )
        self.prediction_mode_: str = mode
        self.threshold_: float = float(self.params.get("threshold", 0.5))
        self.calibrate_classifier_: bool = bool(
            self.params.get("calibrate_classifier", True),
        )
        method = str(
            self.params.get("calibration_method", _DEFAULT_CALIBRATION_METHOD),
        ).strip().lower()
        if method not in _VALID_CALIBRATION_METHODS:
            raise ValueError(
                f"two_stage calibration_method={method!r} is invalid; "
                f"expected one of {list(_VALID_CALIBRATION_METHODS)}"
            )
        self.calibration_method_: str = method
        self.calibration_cv_requested_: int = int(
            self.params.get("calibration_cv", 5),
        )

    # ------------------------------------------------------------------
    # Component builders
    # ------------------------------------------------------------------

    def _make_regressor(self) -> Any:
        reg_name = str(self.params.get("regressor", "lightgbm")).lower()
        reg_params = self.params.get("regressor_params", {}) or {}
        if reg_name == "lightgbm" and _HAS_LGB:
            return LightGBMForecaster(reg_params)
        # ``random_forest`` is the documented fallback when lightgbm is
        # unavailable. We surface a log line so the trainer can see WHY
        # the regressor changed -- silent backend swaps are confusing
        # when comparing model metrics across runs.
        if reg_name == "lightgbm" and not _HAS_LGB:
            logger.warning(
                "two_stage: regressor=lightgbm requested but the optional "
                "dep is unavailable; falling back to random_forest"
            )
        return RandomForestForecaster(reg_params)

    def _base_classifier_pipeline(self) -> Pipeline:
        """LR with StandardScaler. The scaler state is pickled with the
        LR weights so predict() applies the identical normalisation
        that fit() used -- no train/serve skew on feature magnitudes.
        """
        max_iter = int(self.params.get("max_iter", 1000))
        return Pipeline([
            ("scaler", StandardScaler(with_mean=True)),
            ("lr", LogisticRegression(
                max_iter=max_iter,
                class_weight="balanced",
            )),
        ])

    def _resolve_calibration_cv(self, y_bin: np.ndarray) -> int:
        """Adaptive fold count.

        Returns the largest ``cv`` we can run given ``y_bin``, capped
        at the operator-requested value. ``0`` is the sentinel
        for "calibration not possible" (caller skips calibration).
        """
        n_pos = int(y_bin.sum())
        n_neg = int(len(y_bin) - n_pos)
        n_min = min(n_pos, n_neg)
        # Need at least _MIN_SAMPLES_PER_FOLD per class per fold ->
        # cv <= n_min // _MIN_SAMPLES_PER_FOLD.
        max_feasible = n_min // _MIN_SAMPLES_PER_FOLD
        eff = min(self.calibration_cv_requested_, max_feasible)
        return eff if eff >= 2 else 0

    def _wrap_calibrated(self, base: Pipeline, eff_cv: int) -> Any:
        """Build the CalibratedClassifierCV. Handles the sklearn API
        rename (``base_estimator`` -> ``estimator`` in v1.2) so the
        forecaster works against the version range the codebase pins."""
        try:
            return CalibratedClassifierCV(
                estimator=base,
                method=self.calibration_method_,
                cv=eff_cv,
            )
        except TypeError:
            # Older sklearn (<1.2) uses ``base_estimator``.
            return CalibratedClassifierCV(
                base_estimator=base,
                method=self.calibration_method_,
                cv=eff_cv,
            )

    # ------------------------------------------------------------------
    # Fit / predict
    # ------------------------------------------------------------------

    def fit(self, train_df, group_keys, date_col, target_col, feature_cols):
        df = train_df.dropna(subset=[target_col]).copy()
        df["_y_bin"] = (df[target_col] > 0).astype(int)
        X = df[feature_cols].fillna(0.0)
        y_bin = df["_y_bin"].to_numpy()
        min_nonzero_for_regressor = int(
            self.params.get("min_nonzero_for_regressor", 5),
        )

        # --- Stage 1: classifier ---------------------------------------
        # Degenerate label distributions (all-zero or all-positive) have
        # no decision boundary to learn. predict() handles this by
        # treating p_demand=1 so the regressor head passes through.
        n_pos = int(y_bin.sum())
        if n_pos == 0 or n_pos == len(y_bin):
            logger.info(
                "two_stage: degenerate label distribution "
                "(n_pos=%d, n_total=%d); skipping classifier head",
                n_pos, len(y_bin),
            )
            self.classifier_ = None
        else:
            self.classifier_ = self._fit_classifier(X, y_bin)

        # --- Stage 2: regressor on positive rows -----------------------
        nz = df[df[target_col] > 0]
        if len(nz) >= min_nonzero_for_regressor:
            self.regressor_ = self._make_regressor()
            self.regressor_.fit(nz, group_keys, date_col, target_col, feature_cols)
            self._fallback_value_ = None
        else:
            # Not enough positive rows to fit a regressor. Fall back to
            # the unconditional train-window mean -- a stable point
            # estimate the EV combine multiplies by p_demand for a
            # coherent forecast even on tiny pairs.
            logger.info(
                "two_stage: only %d nonzero rows (< %d); skipping "
                "regressor head, falling back to train-window mean",
                len(nz), min_nonzero_for_regressor,
            )
            self.regressor_ = None
            self._fallback_value_ = (
                float(df[target_col].mean()) if len(df) else 0.0
            )

        self.fitted_ = True
        return self

    def _fit_classifier(self, X, y_bin: np.ndarray) -> Any:
        """Build + fit the calibrated classifier with graceful fallback.

        Three tiers of behaviour, in priority order:

          1. Calibrated pipeline (default). Tries the requested cv; if
             cross-validation isn't feasible (minority class too small
             for stratification), drops to tier 2.
          2. Uncalibrated pipeline (scaler + LR). Used when calibration
             was disabled by config OR by the adaptive-cv guard.
          3. Bare LogisticRegression. Reached only when the pipeline
             itself raises -- a last-ditch fallback so training never
             dies on a classifier exception.
        """
        base = self._base_classifier_pipeline()

        # Tier 1: calibrated pipeline (when enabled AND data permits)
        if self.calibrate_classifier_:
            eff_cv = self._resolve_calibration_cv(y_bin)
            if eff_cv >= 2:
                try:
                    clf = self._wrap_calibrated(base, eff_cv)
                    clf.fit(X, y_bin)
                    logger.info(
                        "two_stage: calibrated classifier fit "
                        "(method=%s, cv=%d, n_pos=%d, n_neg=%d)",
                        self.calibration_method_, eff_cv,
                        int(y_bin.sum()), int(len(y_bin) - y_bin.sum()),
                    )
                    return clf
                except Exception as exc:
                    logger.warning(
                        "two_stage: calibrated fit failed (%s); falling "
                        "back to uncalibrated pipeline", exc,
                    )
            else:
                logger.info(
                    "two_stage: minority class size too small for "
                    "cv-based calibration (requested cv=%d, eff_cv=%d); "
                    "fitting uncalibrated pipeline",
                    self.calibration_cv_requested_, eff_cv,
                )

        # Tier 2: uncalibrated pipeline (scaler + LR)
        try:
            base.fit(X, y_bin)
            return base
        except Exception as exc:
            logger.warning(
                "two_stage: pipeline fit failed (%s); falling back to "
                "bare LogisticRegression (no scaling)", exc,
            )

        # Tier 3: bare LR. Last-ditch -- accept the train/serve skew
        # risk over a crashed training run.
        bare = LogisticRegression(
            max_iter=int(self.params.get("max_iter", 1000)),
            class_weight="balanced",
        )
        bare.fit(X.values if hasattr(X, "values") else X, y_bin)
        return bare

    def predict(self, test_df, group_keys, date_col, target_col, feature_cols):
        df = test_df.copy()
        X = df[feature_cols].fillna(0.0)

        # Classifier head -- ``predict_proba`` is uniform across
        # CalibratedClassifierCV, Pipeline(StandardScaler+LR), and the
        # bare LogisticRegression fallback, so this branch needs no
        # type-dispatch.
        if self.classifier_ is not None:
            p_demand = self.classifier_.predict_proba(X)[:, 1]
        else:
            # No classifier (degenerate label distribution at fit). The
            # regressor head's output is the final prediction.
            p_demand = np.ones(len(df))

        # Regressor head -- conditional E[y | y > 0, X].
        if self.regressor_ is not None:
            qty_pred = self.regressor_.predict(
                df, group_keys, date_col, target_col, feature_cols,
            )["prediction"].to_numpy(dtype=float)
        else:
            qty_pred = np.full(
                len(df), float(getattr(self, "_fallback_value_", 0.0) or 0.0),
                dtype=float,
            )

        # Combine the two heads. ``getattr`` defaults preserve
        # backward compatibility: pickles from BEFORE the EV redesign
        # carry no ``prediction_mode_`` attribute, so predict falls
        # back to the OLD threshold behaviour for them. Retraining
        # under the new code stamps ``prediction_mode_="expected_value"``
        # on the artifact and switches to the EV path. This avoids a
        # silent behaviour change on already-deployed models.
        mode = getattr(self, "prediction_mode_", "threshold")
        if mode == "expected_value":
            # E[y | X] = P(y > 0 | X) * E[y | y > 0, X]
            final = p_demand * qty_pred
        else:
            threshold = float(getattr(
                self, "threshold_",
                float(self.params.get("threshold", 0.5)),
            ))
            final = np.where(p_demand >= threshold, qty_pred, 0.0)

        # NaN/inf safety. The classifier or regressor head can emit
        # NaN on degenerate inputs (e.g. all-zero variance features in
        # a small batch); we clip those to 0 so the downstream
        # consumer never serves NaN. ``np.where(isfinite, x, 0)`` is
        # vectorised and handles +/-inf the same way.
        final = np.where(np.isfinite(final), final, 0.0)
        final = np.clip(final, a_min=0.0, a_max=None)

        out = df[group_keys + [date_col]].copy()
        out["prediction"] = final
        out["p_demand"] = p_demand
        out["qty_if_demand"] = qty_pred
        return out
