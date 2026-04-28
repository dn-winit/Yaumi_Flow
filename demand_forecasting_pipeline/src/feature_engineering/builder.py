"""
Feature builder — orchestrates all feature modules.

Split into two phases:

  1. Per-class features that depend on target + demand class (lags, rolling,
     intermittent-specific). Computed on the class-subset of the panel.

  2. Global date-driven features that apply to every row identically
     (temporal, holidays, hijri, salary cycle) and the target encoding
     merge. Computed once on the concatenated panel — no per-class
     duplication.

NaN policy
----------
Lag and rolling features have leading NaN per pair — the "prior" doesn't
exist for the first few rows. Intermittent-specific features behave the same
way. After assembly we fill those columns with ``fe_cfg.nan_fill`` (default
``0.0``, meaning "no prior observation"). Set ``nan_fill: null`` to preserve
NaN for model backends that handle it natively (LightGBM, XGBoost).

Every other family (temporal, holiday, hijri, salary cycle, target encoding)
is fully populated by construction and not touched by the fill pass.
"""

from __future__ import annotations

import logging

import pandas as pd

from .hijri_features import add_hijri_features
from .holiday_features import add_holiday_features
from .lag_features import add_lag_features
from .rolling_features import add_intermittent_features, add_rolling_features
from .salary_cycle import add_salary_cycle_features
from .target_encoding import apply_target_encoding, compute_target_encoding
from .temporal_features import add_temporal_features

logger = logging.getLogger(__name__)

_INTERMITTENT_FEATURE_COLS = (
    "inter_demand_interval",
    "last_nonzero_gap",
    "nonzero_mean_to_date",
)

# Proximity features can legitimately be NaN when there is no event before /
# after a given row (e.g., the horizon extends past the last known holiday).
# We fill those NaNs with a large sentinel so non-NaN-tolerant models (Linear,
# some wrappers) don't crash, without pretending that "no event" is "event
# today". Tree models still learn a threshold against the sentinel.
_PROXIMITY_COL_PREFIXES = ("days_to_next_", "days_since_last_")
_DEFAULT_PROXIMITY_FILL = 9_999.0


def _pair_history_lengths(df, group_keys, target_col):
    return df.groupby(group_keys)[target_col].transform("count").astype(int)


def _horizon_safe_lags(lags: list[int], forecast_horizon: int) -> list[int]:
    """Keep only lags whose reference point stays strictly before the test
    window at every test row. With test_horizon ``H``, a lag ``L`` is
    leakage-safe for the furthest test row iff ``L >= H``.

    The guarantee: at inference we predict ``H`` periods into the future; only
    lags that reference data from before the forecast origin can be computed
    from actuals. Lags < H would require predictions-of-predictions — safe in
    a recursive scheme, not in the current direct-lookup pipeline.
    """
    if forecast_horizon <= 1:
        return list(lags)
    return [lag for lag in lags if lag >= forecast_horizon]


def _build_per_class_features(
    sub: pd.DataFrame,
    *,
    group_keys: list[str],
    date_col: str,
    target_col: str,
    cls: str,
    fe_cfg: dict,
    forecast_horizon: int = 1,
) -> pd.DataFrame:
    """Target-dependent features: lags, rollings, intermittent-specific.

    When ``fe_cfg.horizon_safe_lags`` is true and ``forecast_horizon > 1``,
    the lag list is filtered to horizon-safe entries so the test-window
    metric reflects what inference can actually compute (no use of target
    values that would only be known in hindsight).
    """
    out = sub.sort_values(group_keys + [date_col]).copy()

    if fe_cfg.get("adaptive_depth", False):
        out["pair_history_len"] = _pair_history_lengths(out, group_keys, target_col)

    lags = fe_cfg["lags"].get(cls, fe_cfg["lags"].get("smooth", []))
    if fe_cfg.get("horizon_safe_lags", False):
        safe_lags = _horizon_safe_lags(list(lags), forecast_horizon)
        if len(safe_lags) != len(lags):
            logger.info(
                "Class %s: horizon_safe_lags filtered %d -> %d lags "
                "(forecast_horizon=%d)",
                cls, len(lags), len(safe_lags), forecast_horizon,
            )
        lags = safe_lags
    out = add_lag_features(out, group_keys, target_col, lags)

    roll_cfg = fe_cfg["rolling"].get(cls, fe_cfg["rolling"].get("smooth"))
    out = add_rolling_features(
        out, group_keys, target_col,
        roll_cfg["windows"], roll_cfg["stats"],
        min_periods_fraction=roll_cfg.get("min_periods_fraction", 0.5),
    )

    if cls in ("intermittent", "lumpy"):
        out = add_intermittent_features(
            out, group_keys, target_col, fe_cfg.get("intermittent_specific", {})
        )
    return out


def _apply_nan_policy(df: pd.DataFrame, nan_fill, proximity_fill) -> pd.DataFrame:
    """Fill NaN on feature families so downstream models never see NaN.

    Two separate policies because the semantics differ:

      - Target-derived (``lag_*``, ``roll_*``, intermittent): NaN means "no
        prior observation"; filling with 0 treats pre-launch rows as zero
        demand.
      - Proximity (``days_to_next_*`` / ``days_since_last_*``): NaN means
        "no such event in the known range"; filling with a large sentinel
        (default 9999) lets the model distinguish "event is far" from
        "event is today".

    ``nan_fill=None`` preserves NaN for NaN-native model backends.
    """
    target_families = [
        c for c in df.columns
        if c.startswith("lag_")
        or c.startswith("roll_")
        or c in _INTERMITTENT_FEATURE_COLS
    ]
    if nan_fill is not None and target_families:
        df[target_families] = df[target_families].fillna(nan_fill)

    prox_cols = [
        c for c in df.columns
        if any(c.startswith(p) for p in _PROXIMITY_COL_PREFIXES)
    ]
    if proximity_fill is not None and prox_cols:
        df[prox_cols] = df[prox_cols].fillna(proximity_fill)

    return df


def build_features(
    df: pd.DataFrame,
    group_keys: list[str],
    date_col: str,
    target_col: str,
    classes_df: pd.DataFrame,
    fe_cfg: dict,
    *,
    holiday_cfg: dict | None = None,
    hijri_cfg: dict | None = None,
    salary_cycle_cfg: dict | None = None,
    granularity: str = "D",
    full_date_range=None,
    target_encoding_source: pd.DataFrame | None = None,
    forecast_horizon: int = 1,
) -> pd.DataFrame:
    """Top-level feature assembly.

    Order:
      1. Join ``class`` onto panel so per-class features know which bucket
         each row belongs to.
      2. Per-class features (lags, rolling, intermittent).
      3. Concat back into a single panel.
      4. Global date-driven features (once — no per-class duplication).
      5. Target encoding from the train-window source.
      6. NaN policy on target-derived features.
    """
    merged = df.merge(
        classes_df.reset_index()[group_keys + ["class"]],
        on=group_keys, how="left",
    )
    # Any pair in df that wasn't classified gets dropped explicitly — with
    # a log entry so the loss is visible, not silent.
    missing_class = int(merged["class"].isna().sum())
    if missing_class:
        affected_pairs = int(
            merged.loc[merged["class"].isna()].groupby(group_keys).ngroups
        )
        logger.warning(
            "build_features: dropping %d rows across %d pairs with no class "
            "(likely test-window-only pairs the classifier did not see)",
            missing_class, affected_pairs,
        )
        merged = merged.loc[merged["class"].notna()].copy()

    parts: list[pd.DataFrame] = []
    for cls, sub in merged.groupby("class", sort=False):
        if sub.empty:
            continue
        parts.append(
            _build_per_class_features(
                sub,
                group_keys=group_keys, date_col=date_col, target_col=target_col,
                cls=cls, fe_cfg=fe_cfg,
                forecast_horizon=forecast_horizon,
            )
        )
    out = pd.concat(parts, ignore_index=True) if parts else merged

    # --- Global date-driven features (apply once) -------------------------
    temporal_cfg = fe_cfg.get("temporal", {}) or {}
    if temporal_cfg.get("enabled", True):
        out = add_temporal_features(
            out, date_col, temporal_cfg,
            group_keys=group_keys, granularity=granularity,
        )

    if holiday_cfg and holiday_cfg.get("enabled", False):
        out = add_holiday_features(
            out, date_col, granularity, holiday_cfg,
            full_date_range=full_date_range,
        )

    if hijri_cfg:
        out = add_hijri_features(out, date_col, granularity, hijri_cfg)

    if salary_cycle_cfg:
        out = add_salary_cycle_features(out, date_col, granularity, salary_cycle_cfg)

    # --- Target encoding (leakage-free: source is train-window only) ------
    te_cfg = fe_cfg.get("target_encoding", {}) or {}
    if te_cfg.get("enabled", False) and target_encoding_source is not None:
        smoothing = int(te_cfg.get("smoothing", 10))
        te_df, global_mean = compute_target_encoding(
            target_encoding_source, group_keys, target_col, smoothing
        )
        out = apply_target_encoding(out, group_keys, te_df, global_mean)

    # --- NaN policy: fill target-derived and proximity families ----------
    # Surface the chosen fills explicitly so downstream readers know which
    # assumption is in force. ``nan_fill=0.0`` treats pre-launch periods as
    # "no prior observation = zero demand" — a strong assumption for
    # intermittent classes; users wanting the model to see NaN should set
    # ``feature_engineering.nan_fill: null``.
    nan_fill_explicit = "nan_fill" in (fe_cfg or {})
    proximity_explicit = "proximity_nan_fill" in (fe_cfg or {})
    nan_fill = fe_cfg.get("nan_fill", 0.0)
    proximity_fill = fe_cfg.get("proximity_nan_fill", _DEFAULT_PROXIMITY_FILL)
    if not nan_fill_explicit or not proximity_explicit:
        logger.info(
            "build_features NaN fills: target_families=%s (%s), proximity=%s (%s)",
            nan_fill, "config" if nan_fill_explicit else "default",
            proximity_fill, "config" if proximity_explicit else "default",
        )
    out = _apply_nan_policy(out, nan_fill, proximity_fill)

    return out
