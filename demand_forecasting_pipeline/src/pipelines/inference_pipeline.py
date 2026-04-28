"""
Inference pipeline — produce future forecasts from persisted training artifacts.

Alignment contract with training
--------------------------------
The data-processing transforms applied here are **the same functions training
uses, in the same order**, so the feature matrix this pipeline produces for
rows present in the training panel is numerically identical to the matrix the
models were fit on. The only divergences are intentional:

  * inference loads persisted ``pair_classes.csv`` / ``outlier_bounds.csv``
    instead of recomputing (same values by construction).
  * inference builds a future skeleton (horizon rows per pair) appended to
    the historical panel so ``build_features`` can compute lags / rollings
    / calendar features for the forecast horizon.

For horizon > 1 the future rows carry ``target = 0`` (we don't know the real
future). Lag and rolling features at positions ≥ 2 of the horizon therefore
reference those placeholder zeros, not real actuals — a direct-lookup bias
that's honest when ``feature_engineering.horizon_safe_lags`` is true (which
filters lags < horizon) and optimistic when it's false. The flag is surfaced
loudly in the log so consumers of the forecast know which regime they're in.
"""

from __future__ import annotations

import logging
import os

import pandas as pd

from ..data_processing.aggregator import build_panel
from ..data_processing.anomaly import detect_suspicious_zeros
from ..data_processing.cleaner import apply_outlier_bounds, clip_negative_to_zero
from ..data_processing.lifecycle import assign_lifecycle_flags
from ..data_processing.loader import load_raw
from ..data_processing.validator import validate_input
from ..feature_engineering.builder import build_features
from ..models.ensemble import weighted_average_ensemble
from ..utils.config_loader import ensure_dirs, load_config, resolve_dtypes
from ..utils.io_utils import ensure_tuple, load_json, load_pickle, pair_mask, save_dataframe
from ..utils.logger import get_logger
from ..utils.time_utils import build_date_range, period_offset


def _future_skeleton(history_df, group_keys, date_col, horizon, granularity):
    """One row per (pair, horizon step). Each row is (pair_keys + future_date).
    Target / causal / meta columns are filled later."""
    rows = []
    for keys, g in history_df.groupby(group_keys):
        last = g[date_col].max()
        for h in range(1, horizon + 1):
            row = {date_col: last + period_offset(granularity, h)}
            for k, v in zip(group_keys, ensure_tuple(keys)):
                row[k] = v
            rows.append(row)
    return pd.DataFrame(rows)


def _resolve_fallback_model(cfg: dict) -> str:
    """Model name used when no pair-specific winner is available. Reads from
    config so changing ``models.fallback_order`` actually takes effect at
    inference — no hardcoded sentinels."""
    fallback_order = cfg.get("models", {}).get("fallback_order") or []
    return fallback_order[0] if fallback_order else "moving_average"


def _fold_predictions_into_panel(
    full: pd.DataFrame,
    forecast: pd.DataFrame,
    *,
    group_keys: list,
    date_col: str,
    target_col: str,
    history_max_date,
) -> pd.DataFrame:
    """Write predicted values back into ``full[target_col]`` for future-window
    rows so the next ``build_features`` pass sees them as if observed.

    Used by recursive multi-step inference: after a forward pass produces
    forecasts for periods t+1..t+H, those values become the *target* for
    feature-engineering purposes on the next pass — so lag_1 at t+2 sees the
    prediction at t+1, lag_7 at t+8 sees the prediction at t+1, and so on.

    History rows (date <= ``history_max_date``) are NEVER touched — the
    model fit's ground truth for the past is preserved. Pairs absent from
    ``forecast`` keep their placeholder zeros (cold-start in this pass).
    """
    if forecast.empty or "prediction" not in forecast.columns:
        return full
    fold = forecast[group_keys + [date_col, "prediction"]].rename(
        columns={"prediction": "_pred_target"}
    )
    out = full.merge(fold, on=group_keys + [date_col], how="left")
    mask = (out[date_col] > history_max_date) & out["_pred_target"].notna()
    if mask.any():
        out.loc[mask, target_col] = pd.to_numeric(
            out.loc[mask, "_pred_target"], errors="coerce",
        ).astype(float).values
    return out.drop(columns=["_pred_target"])


def run_inference(config_path, on_step=None):
    _step = on_step or (lambda *_: None)

    cfg = load_config(config_path)
    ensure_dirs(cfg)
    logger = get_logger("inference", cfg["paths"]["logs_dir"], cfg["project"]["log_level"])

    date_col = cfg["data"]["date_col"]
    target_col = cfg["data"]["target_col"]
    group_keys = cfg["data"]["forecast_level"]
    meta_cols = cfg["data"].get("meta_cols", [])
    freq = cfg["data"]["granularity"]
    causal_cols = cfg["data"].get("causal_cols", [])
    activity_flag = cfg["data"].get("activity_flag", False)

    horizon = int(cfg["inference"].get("forecast_horizon", 0))
    if horizon <= 0:
        raise ValueError(
            f"inference.forecast_horizon must be > 0 to produce a future "
            f"forecast; got {horizon}"
        )

    # Multi-step lag handling. Three regimes:
    #   1. ``inference.recursive_iterations > 0`` (default 1): predict, fold
    #      predictions back into the panel, rebuild lag/rolling features, and
    #      re-predict — the standard recursive multi-step approach. No bias.
    #   2. ``feature_engineering.horizon_safe_lags = true``: drop lags shorter
    #      than ``forecast_horizon`` from the feature matrix entirely. No
    #      bias, but loses informative recent-history features.
    #   3. Both off: lag features for periods t+2..t+H reference the
    #      placeholder zeros in the future skeleton — biased toward
    #      under-forecasting. Loud warning so this regime can't sneak in.
    horizon_safe = bool(cfg.get("feature_engineering", {}).get("horizon_safe_lags", False))
    recursive_iters = int(cfg.get("inference", {}).get("recursive_iterations", 1))
    if horizon > 1 and not horizon_safe and recursive_iters <= 0:
        # Hard fail rather than warn — this combination silently produces
        # biased multi-step forecasts (lag features beyond t+1 reference
        # placeholder zeros). The user must opt in to one of the two
        # honest regimes before inference can proceed.
        raise RuntimeError(
            f"Refusing to run multi-step inference (horizon={horizon}) with both "
            f"feature_engineering.horizon_safe_lags=False AND "
            f"inference.recursive_iterations=0. This combination produces a "
            f"silently biased forecast (short lags reference placeholder zeros "
            f"for periods t+2..t+H). Set inference.recursive_iterations >= 1 "
            f"(recommended) or feature_engineering.horizon_safe_lags=true."
        )

    _step("data_collection", "running")
    logger.info("Loading data for inference")
    # Single dtype map, reused for every CSV read — raw feed and every
    # persisted artifact. Guarantees identical types across the
    # training → artifact → inference round-trip.
    dtypes = resolve_dtypes(cfg)
    raw = load_raw(cfg["paths"]["raw_data"], date_col, dtypes=dtypes)

    # Validate the raw feed the same way training does. Findings are logged
    # but don't halt inference — the aggregator handles duplicates by sum.
    validation_report = validate_input(raw, cfg)
    logger.info(
        "Inference validation: rows=%d bad_dates=%d duplicate_rows=%d future_dates=%d",
        validation_report.n_rows, validation_report.bad_dates,
        validation_report.duplicate_rows, validation_report.future_dates,
    )

    all_cols = list(dict.fromkeys(
        group_keys + [date_col, target_col] + (meta_cols or []) +
        [cc["col"] for cc in causal_cols if cc["col"] in raw.columns]
    ))
    raw = raw[[c for c in all_cols if c in raw.columns]].copy()

    agg = build_panel(
        raw, group_keys, date_col, target_col, meta_cols, freq,
        fill_missing=cfg["data"].get("fill_missing_periods", True),
        fill_value=cfg["data"].get("zero_fill_value", 0.0),
        causal_cols=causal_cols,
        activity_flag=activity_flag,
    )

    _step("data_collection", "completed")
    _step("data_processing", "running")

    classes_path = os.path.join(cfg["paths"]["explainability_dir"], "pair_classes.csv")
    if not os.path.exists(classes_path):
        raise RuntimeError(f"Missing {classes_path} — run training first")
    classes_df = pd.read_csv(classes_path, dtype=dtypes).set_index(group_keys)

    # Pairs present in the feed but never seen during training can't be
    # forecast by the saved per-class models — report and drop explicitly.
    pairs_before = agg.groupby(group_keys).ngroups
    agg = agg.merge(classes_df.reset_index()[group_keys], on=group_keys, how="inner")
    pairs_after = agg.groupby(group_keys).ngroups
    unknown_pairs = pairs_before - pairs_after
    if unknown_pairs:
        logger.warning(
            "Inference: dropping %d pair(s) not present in pair_classes.csv "
            "(cold-start pairs have no trained model). Retrain to cover them.",
            unknown_pairs,
        )

    # Apply training's data-processing stack in the same order. Steps are
    # deterministic functions of the historical target, so re-deriving here
    # yields identical flags on the same rows training saw. The outlier
    # clipper uses persisted bounds (NOT re-derived from agg) so clipping
    # points match training exactly.
    if cfg["cleaning"].get("clip_negative_to_zero", True):
        agg = clip_negative_to_zero(agg, target_col)

    lifecycle_cfg = cfg.get("lifecycle", {}) or {}
    if lifecycle_cfg.get("enabled", True):
        agg = assign_lifecycle_flags(
            agg, group_keys, target_col, date_col,
            new_launch_periods=int(lifecycle_cfg.get("new_launch_periods", 0)),
            eol_silent_periods=int(lifecycle_cfg.get("eol_silent_periods", 0)),
        )

    anomaly_cfg = (cfg.get("anomaly", {}) or {}).get("suspicious_zeros", {}) or {}
    if anomaly_cfg.get("enabled", True):
        agg = detect_suspicious_zeros(
            agg, group_keys, target_col, date_col,
            min_history_periods=int(anomaly_cfg.get("min_history_periods", 60)),
            min_zero_runs=int(anomaly_cfg.get("min_zero_runs", 3)),
            z_threshold=float(anomaly_cfg.get("z_threshold", 3.0)),
            skip_flag_cols=["is_new_launch", "is_likely_eol"],
        )

    outlier_cfg = cfg["cleaning"].get("per_pair_outlier", {}) or {}
    bounds_path = os.path.join(cfg["paths"]["models_dir"], "outlier_bounds.csv")
    if outlier_cfg.get("enabled", False) and os.path.exists(bounds_path):
        outlier_bounds = pd.read_csv(bounds_path, dtype=dtypes)
        pre_rows = len(agg)
        agg = apply_outlier_bounds(
            agg, outlier_bounds, group_keys, target_col, outlier_cfg, classes=classes_df,
        )
        logger.info(
            "Applied persisted outlier bounds to %d rows (bounds defined for %d pairs)",
            pre_rows, len(outlier_bounds),
        )
    elif outlier_cfg.get("enabled", False):
        logger.warning(
            "Outlier treatment enabled in config but %s is missing — "
            "inference features will differ from training. Retrain to regenerate.",
            bounds_path,
        )

    # ---- Future skeleton ------------------------------------------------
    # One row per (pair, step) for the forecast horizon. Target is set to
    # 0 because we don't know the future; meta / causal columns are carried
    # forward from each pair's most recent history so feature code that
    # consumes them (e.g. vwap price lags) has a value to work with. This is
    # the standard direct-forecasting setup — no manipulation beyond what's
    # necessary to run feature_engineering on rows without observed target.
    future = _future_skeleton(agg, group_keys, date_col, horizon, freq)
    future[target_col] = 0.0
    meta_present = [c for c in (meta_cols or []) if c in agg.columns]
    causal_present = [cc["col"] for cc in causal_cols if cc["col"] in agg.columns]
    if meta_present or causal_present:
        fill_cols = meta_present + causal_present
        fill_df = (
            agg.sort_values(date_col)
            .groupby(group_keys, as_index=False)[fill_cols].last()
        )
        future = future.merge(fill_df, on=group_keys, how="left")
    if activity_flag and "activity_flag" not in future.columns:
        # Activity flag doesn't flow into any feature — it's a pass-through
        # column consumed by downstream dashboards. 0 denotes "no observation
        # (inferred)" which is the honest label for a forecast row.
        future["activity_flag"] = 0
    full = pd.concat([agg, future], ignore_index=True, sort=False)
    full_date_range = build_date_range(full[date_col].min(), full[date_col].max(), freq)

    _step("data_processing", "completed")
    _step("feature_engineering", "running")

    feats = build_features(
        full, group_keys, date_col, target_col, classes_df, cfg["feature_engineering"],
        holiday_cfg=cfg.get("holidays"),
        hijri_cfg=cfg.get("hijri"),
        salary_cycle_cfg=cfg.get("salary_cycle"),
        granularity=freq,
        full_date_range=full_date_range,
        target_encoding_source=agg,
        forecast_horizon=horizon,
    )
    _step("feature_engineering", "completed")

    summary = load_json(os.path.join(cfg["paths"]["artifacts_dir"], "training_summary.json"))
    schema = summary.get("schema", {})
    feature_cols = schema.get("feature_cols") or []
    per_class_feature_cols = schema.get("per_class_feature_cols") or {}
    if not feature_cols:
        raise RuntimeError("training_summary.json missing schema.feature_cols; retrain required")

    # Pre-materialize any column the training run emitted that isn't in the
    # current feats frame (e.g. a holiday slug the library no longer returns).
    # Fill with zero so models never see KeyError.
    missing = [c for c in feature_cols if c not in feats.columns]
    for c in missing:
        feats[c] = 0.0
    if missing:
        logger.info("Added %d missing feature cols: %s", len(missing), missing[:10])

    # Safety net for any residual NaN the builder's nan_policy didn't catch.
    remaining = feats[feature_cols].isna().sum()
    nan_cols = remaining[remaining > 0]
    if not nan_cols.empty:
        logger.warning(
            "Inference: %d feature columns had residual NaN after build_features "
            "(filling with 0 as last resort). Columns: %s",
            len(nan_cols), list(nan_cols.index)[:10],
        )
        feats[nan_cols.index.tolist()] = feats[nan_cols.index.tolist()].fillna(0.0)

    lookup_path = os.path.join(cfg["paths"]["artifacts_dir"], "pair_model_lookup.csv")
    per_pair = cfg.get("model_selection", {}).get("strategy", "per_class") == "per_pair"
    lookup = None
    if per_pair and os.path.exists(lookup_path):
        lookup = pd.read_csv(lookup_path, dtype=dtypes)
        logger.info("Loaded per-pair model lookup (%d pairs)", len(lookup))

    fallback_model = _resolve_fallback_model(cfg)

    _step("inference", "running")
    per_class = summary["per_class"]

    # Pre-slice future once per class — reused for main prediction AND for
    # the quantile interval loop below, so we don't merge twice.
    future_by_class: dict[str, pd.DataFrame] = {}
    for cls in per_class:
        cls_pairs = classes_df[classes_df["class"] == cls].reset_index()[group_keys]
        cls_future = future.merge(cls_pairs, on=group_keys, how="inner")
        if not cls_future.empty:
            future_by_class[cls] = cls_future

    # Pre-load every per-class artifact ONCE. Recursive iterations re-use the
    # same loaded model weights with a refreshed feature matrix, so loading
    # the pickles inside the loop would just burn I/O for no gain.
    class_artifacts: dict[str, dict] = {}
    for cls, info in per_class.items():
        if cls not in future_by_class:
            continue
        cls_future = future_by_class[cls]
        cls_pairs = cls_future[group_keys].drop_duplicates()
        cls_feature_cols = per_class_feature_cols.get(cls) or feature_cols

        if lookup is not None:
            cls_lookup = lookup[lookup["class"] == cls]
            models_needed = list(cls_lookup["best_model"].unique())
        else:
            cls_lookup = None
            models_needed = (
                [info["models_trained"][0]] if info.get("models_trained") else [fallback_model]
            )

        models_to_load = set(models_needed)
        ens_weights_path = os.path.join(cfg["paths"]["models_dir"], f"{cls}_ensemble_weights.json")
        ens_weights = None
        if "ensemble" in models_to_load and os.path.exists(ens_weights_path):
            ens_weights = load_json(ens_weights_path)
            models_to_load.discard("ensemble")
            models_to_load.update(ens_weights.keys())

        loaded_models: dict = {}
        for m_name in models_to_load:
            model_path = os.path.join(cfg["paths"]["models_dir"], f"{cls}_{m_name}.pkl")
            if not os.path.exists(model_path):
                logger.warning(
                    "Missing pickle for %s/%s at %s — pairs using it will fall back",
                    cls, m_name, model_path,
                )
                continue
            try:
                loaded_models[m_name] = load_pickle(model_path)
            except Exception as exc:
                logger.warning("Failed to load pickle %s/%s: %s", cls, m_name, exc)

        class_artifacts[cls] = {
            "cls_pairs": cls_pairs,
            "cls_feature_cols": cls_feature_cols,
            "cls_lookup": cls_lookup,
            "models_needed": models_needed,
            "loaded_models": loaded_models,
            "ens_weights": ens_weights,
        }

    def _run_point_pass(current_feats: pd.DataFrame) -> pd.DataFrame:
        """One forward pass: predict every (class, pair) using the current
        feature matrix. Pre-loaded models are reused across iterations."""
        out_parts: list[pd.DataFrame] = []
        for cls, art in class_artifacts.items():
            cls_future = future_by_class[cls]
            cls_feats = current_feats.merge(
                cls_future[group_keys + [date_col]], on=group_keys + [date_col], how="inner",
            )
            for _, pair_row in art["cls_pairs"].iterrows():
                pair_keys = tuple(pair_row[k] for k in group_keys)
                pair_feats = cls_feats[pair_mask(cls_feats, group_keys, pair_keys)]
                if pair_feats.empty:
                    continue

                if art["cls_lookup"] is not None:
                    lk_row = art["cls_lookup"][pair_mask(art["cls_lookup"], group_keys, pair_keys)]
                    best = lk_row["best_model"].iloc[0] if not lk_row.empty else fallback_model
                else:
                    best = art["models_needed"][0]

                preds = _predict_for_pair(
                    best, art["loaded_models"], art["ens_weights"], pair_feats, pair_keys, agg,
                    group_keys, date_col, target_col, art["cls_feature_cols"], cfg,
                    logger, cls, fallback_model,
                )

                if "p_demand" not in preds.columns:
                    preds["p_demand"] = (preds["prediction"] > 0).astype(float)
                    preds["qty_if_demand"] = preds["prediction"]
                preds["class"] = cls
                preds["model_used"] = best
                out_parts.append(preds)

        return pd.concat(out_parts, ignore_index=True) if out_parts else pd.DataFrame()

    # ---- Initial direct pass ------------------------------------------------
    forecast = _run_point_pass(feats)

    # ---- Recursive multi-step refinement ------------------------------------
    # For each iteration: fold predictions back into the panel target column
    # for future rows, rebuild features (so lag/rolling windows now see
    # predicted values from the prior pass), and re-predict. 1 iteration is
    # usually sufficient to capture short-lag dependencies; 2+ for longer
    # horizons where lags compose multiple forecast steps.
    if recursive_iters > 0 and horizon > 1 and not forecast.empty:
        history_max = agg[date_col].max()
        for it in range(recursive_iters):
            full = _fold_predictions_into_panel(
                full, forecast,
                group_keys=group_keys, date_col=date_col, target_col=target_col,
                history_max_date=history_max,
            )
            feats = build_features(
                full, group_keys, date_col, target_col, classes_df, cfg["feature_engineering"],
                holiday_cfg=cfg.get("holidays"),
                hijri_cfg=cfg.get("hijri"),
                salary_cycle_cfg=cfg.get("salary_cycle"),
                granularity=freq,
                full_date_range=full_date_range,
                target_encoding_source=agg,
                forecast_horizon=horizon,
            )
            # Re-apply the safety nets: missing columns and residual NaN.
            for c in feature_cols:
                if c not in feats.columns:
                    feats[c] = 0.0
            feats[feature_cols] = feats[feature_cols].fillna(0.0)
            forecast = _run_point_pass(feats)
            logger.info(
                "Recursive multi-step iteration %d/%d complete (rows=%d)",
                it + 1, recursive_iters, len(forecast),
            )

    # Quantile prediction intervals (optional, per-class).
    pi_cfg = cfg.get("evaluation", {}).get("prediction_intervals", {})
    if pi_cfg.get("enabled", False) and not forecast.empty:
        qi_parts = []
        for cls in per_class:
            cls_future = future_by_class.get(cls)
            if cls_future is None:
                continue
            qi_path = os.path.join(cfg["paths"]["models_dir"], f"quantile_{cls}.pkl")
            if not os.path.exists(qi_path):
                continue
            try:
                qi_model = load_pickle(qi_path)
                cls_future_feats = feats.merge(
                    cls_future[group_keys + [date_col]], on=group_keys + [date_col], how="inner",
                )
                if cls_future_feats.empty:
                    continue
                qi_cols_to_use = per_class_feature_cols.get(cls) or feature_cols
                qi_pred = qi_model.predict(
                    cls_future_feats, group_keys, date_col, target_col, qi_cols_to_use,
                )
                qi_parts.append(qi_pred)
            except Exception as exc:
                logger.warning("Quantile inference failed for %s: %s", cls, exc)
        if qi_parts:
            qi_all = pd.concat(qi_parts, ignore_index=True)
            qi_cols = [c for c in qi_all.columns if c.startswith("q_")]
            if qi_cols:
                for qc in qi_cols:
                    qi_all[qc] = qi_all[qc].clip(lower=0.0)
                if "q_10" in qi_cols and "q_90" in qi_cols:
                    # Quantile rearrangement (Chernozhukov et al. 2010): each
                    # LightGBM quantile model is fit independently and may
                    # cross on individual rows (raw q_10 > raw q_90). Sorting
                    # the pair element-wise restores monotonicity at zero
                    # cost to expected coverage. (The point-in-band widening
                    # happens later, after conformal offsets are applied.)
                    qi_all["q_10"] = qi_all[["q_10", "q_90"]].min(axis=1)
                    qi_all["q_90"] = qi_all[["q_10", "q_90"]].max(axis=1)
                forecast = forecast.merge(
                    qi_all[group_keys + [date_col] + qi_cols],
                    on=group_keys + [date_col], how="left",
                )
                # Conformal calibration: per-pair offsets persisted at train
                # time. Pairs missing from the offsets file (cold-start) get
                # offset=0, i.e. the raw class quantile band — the safe
                # default until a future training run calibrates them.
                conformal_path = os.path.join(cfg["paths"]["artifacts_dir"], "conformal_offsets.csv")
                if os.path.exists(conformal_path):
                    try:
                        from ..evaluation.conformal import apply_pair_offsets
                        offsets = pd.read_csv(conformal_path, dtype=dtypes)
                        forecast = apply_pair_offsets(
                            forecast, offsets, group_keys=group_keys,
                        )
                        logger.info(
                            "Applied conformal offsets to %d forecast rows (offsets for %d pairs)",
                            len(forecast), len(offsets),
                        )
                    except Exception as exc:
                        logger.warning("Conformal offset application failed: %s", exc)
                # Point-in-band guarantee: the point forecast and quantile
                # bands come from independent models. On rare rows the point
                # can fall outside the (calibrated) band — incoherent to a
                # customer ("our most likely number is outside our likely
                # range?"). Widen the band just enough to contain ``pred``
                # whenever this happens. Coverage only increases — never
                # narrowed silently.
                forecast["q_10"] = forecast[["q_10", "prediction"]].min(axis=1).clip(lower=0.0)
                forecast["q_90"] = forecast[["q_90", "prediction"]].max(axis=1)

    # Enrich each forecast row with the pair's explainability stats.
    if not forecast.empty:
        expl_path = os.path.join(cfg["paths"]["explainability_dir"], "pair_explainability.csv")
        if os.path.exists(expl_path):
            expl = pd.read_csv(expl_path, dtype=dtypes)
            forecast = forecast.merge(expl, on=group_keys, how="left", suffixes=("", "_expl"))

    save_dataframe(forecast, os.path.join(cfg["paths"]["predictions_dir"], "future_forecast.csv"))
    _step("inference", "completed")
    logger.info("Inference complete: %d rows", len(forecast))
    return forecast


def _predict_for_pair(
    best_model_name, loaded_models, ens_weights, pair_feats, pair_keys, agg,
    group_keys, date_col, target_col, cls_feature_cols, cfg,
    logger, cls, fallback_model,
):
    """Run a single pair through its chosen model. Encapsulates the
    ensemble branch, the missing-pickle branch, and the predict-failure
    branch so the main loop reads top-to-bottom without nested try/except."""
    if best_model_name == "ensemble" and ens_weights:
        comp_preds: dict = {}
        for comp_name in ens_weights:
            comp_mdl = loaded_models.get(comp_name)
            if comp_mdl is None:
                continue
            try:
                comp_preds[comp_name] = comp_mdl.predict(
                    pair_feats, group_keys, date_col, target_col, cls_feature_cols,
                )
            except Exception as exc:
                logger.warning(
                    "Ensemble component %s/%s predict failed for pair %s: %s",
                    cls, comp_name, pair_keys, exc,
                )
        if comp_preds:
            return weighted_average_ensemble(
                comp_preds,
                {k: float(v) for k, v in ens_weights.items() if k in comp_preds},
            )
        return _zero_prediction(pair_feats, group_keys, date_col)

    mdl = loaded_models.get(best_model_name)
    if mdl is None:
        # No persisted model for this pair's chosen name — fit the config-
        # declared fallback on-the-fly so every pair gets something sensible.
        mdl = _build_fallback_inplace(
            fallback_model, agg, pair_keys, group_keys, date_col, target_col,
        )
        if mdl is None:
            logger.warning(
                "No model available (chosen=%s, fallback=%s) for pair %s — emitting zero forecast",
                best_model_name, fallback_model, pair_keys,
            )
            return _zero_prediction(pair_feats, group_keys, date_col)

    try:
        return mdl.predict(pair_feats, group_keys, date_col, target_col, cls_feature_cols)
    except Exception as exc:
        logger.warning(
            "Predict failed for %s/%s pair %s: %s — emitting zero forecast",
            cls, best_model_name, pair_keys, exc,
        )
        return _zero_prediction(pair_feats, group_keys, date_col)


def _build_fallback_inplace(fallback_model, agg, pair_keys, group_keys, date_col, target_col):
    """Instantiate the config-declared fallback model and fit it on a single
    pair's history. Returns ``None`` if the fallback model isn't available."""
    from ..models.registry import build_model, is_available

    if not is_available(fallback_model):
        return None
    mdl = build_model(fallback_model, {})
    pair_history = agg[pair_mask(agg, group_keys, pair_keys)]
    if pair_history.empty:
        return None
    mdl.fit(pair_history, group_keys, date_col, target_col, [])
    return mdl


def _zero_prediction(pair_feats: pd.DataFrame, group_keys: list[str], date_col: str) -> pd.DataFrame:
    """Last-resort output: keys + date + prediction=0. Logged, not silent."""
    out = pair_feats[group_keys + [date_col]].copy()
    out["prediction"] = 0.0
    return out
