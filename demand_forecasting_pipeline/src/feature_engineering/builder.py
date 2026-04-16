import pandas as pd

from .temporal_features import add_temporal_features
from .lag_features import add_lag_features
from .rolling_features import add_rolling_features, add_intermittent_features
from .holiday_features import add_holiday_features
from .target_encoding import compute_target_encoding, apply_target_encoding


def _pair_history_lengths(df, group_keys, target_col):
    return df.groupby(group_keys)[target_col].transform("count").astype(int)


def build_features_for_class(df, group_keys, date_col, target_col, cls, fe_cfg, granularity="D"):
    df = df.sort_values(group_keys + [date_col]).copy()

    if fe_cfg.get("adaptive_depth", False):
        df["pair_history_len"] = _pair_history_lengths(df, group_keys, target_col)

    if fe_cfg.get("temporal", {}).get("enabled", True):
        comps = fe_cfg["temporal"]["components"]
        weekend_days = fe_cfg["temporal"].get("weekend_days")
        df = add_temporal_features(df, date_col, comps, group_keys=group_keys,
                                   granularity=granularity, weekend_days=weekend_days)

    lags = fe_cfg["lags"].get(cls, fe_cfg["lags"].get("smooth", []))
    df = add_lag_features(df, group_keys, target_col, lags)

    roll_cfg = fe_cfg["rolling"].get(cls, fe_cfg["rolling"].get("smooth"))
    df = add_rolling_features(df, group_keys, target_col, roll_cfg["windows"], roll_cfg["stats"])

    if cls in ("intermittent", "lumpy"):
        df = add_intermittent_features(df, group_keys, target_col, fe_cfg.get("intermittent_specific", {}))
    return df


def build_features(df, group_keys, date_col, target_col, classes_df, fe_cfg,
                   holiday_cfg=None, granularity="D", full_date_range=None,
                   target_encoding_source=None):
    out_parts = []
    merged = df.merge(classes_df.reset_index()[group_keys + ["class"]], on=group_keys, how="left")
    for cls, sub in merged.groupby("class"):
        if sub.empty:
            continue
        feats = build_features_for_class(sub, group_keys, date_col, target_col, cls, fe_cfg, granularity)
        out_parts.append(feats)
    out = pd.concat(out_parts, ignore_index=True)

    if holiday_cfg and holiday_cfg.get("enabled", False):
        out = add_holiday_features(
            out, date_col,
            holiday_cfg.get("country_code"),
            holiday_cfg.get("subdivision"),
            holiday_cfg.get("custom_events", []),
            granularity,
            full_date_range=full_date_range,
        )

    te_cfg = fe_cfg.get("target_encoding", {})
    if te_cfg.get("enabled", False) and target_encoding_source is not None:
        smoothing = int(te_cfg.get("smoothing", 10))
        te_df, global_mean = compute_target_encoding(
            target_encoding_source, group_keys, target_col, smoothing
        )
        out = apply_target_encoding(out, group_keys, te_df, global_mean)

    return out
