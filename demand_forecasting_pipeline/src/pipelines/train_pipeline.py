import os
from pathlib import Path

import numpy as np
import pandas as pd

from ..utils.config_loader import ensure_dirs, load_config, resolve_dtypes
from ..utils.logger import get_logger
from ..utils.io_utils import (
    ceil_int_columns, save_json, save_pickle, save_dataframe, pair_mask,
)
from ..utils.time_utils import period_offset, build_date_range
from ..data_processing.loader import load_raw
from ..data_processing.aggregator import build_panel
from ..data_processing.cleaner import (
    apply_outlier_bounds,
    clip_negative_to_zero,
    fit_outlier_bounds,
)
from ..data_processing.splitter import time_based_split
from ..data_processing.validator import validate_input
from ..data_processing.lifecycle import assign_lifecycle_flags
from ..data_processing.anomaly import detect_suspicious_zeros
from ..data_processing.quality import build_report as build_data_quality_report
from ..data_processing.split_audit import audit_split, summarise as summarise_split_audit
from ..feature_engineering.classifier import classify_dataset
from ..feature_engineering.explainability import compute_pair_explainability
from ..feature_engineering.builder import build_features
from ..models.registry import build_model, is_available
from ..models.ensemble import weighted_average_ensemble, weights_from_metric
from ..tuning.optuna_tuner import tune_model
from ..evaluation.metrics import compute_all, composite_summary
from ..routing import Route, Router, build_pair_signals


# Flag columns must never leak into the feature matrix. They encode
# train-time labels whose inference-time values would be either stale
# (lifecycle flags) or trivially zero (suspicious_zero), and either would
# bias the model.
_FLAG_COLUMNS = ("suspicious_zero", "is_new_launch", "is_likely_eol")
META_FEATURE_EXCLUDE = {"class", *_FLAG_COLUMNS}

# Definitional seasonal cycle lengths per pandas freq letter. These are
# facts (weekdays per week, months per year, ...), not tuning choices.
_SEASONAL_PERIODS_BY_FREQ = {"D": 7, "W": 52, "M": 12, "Q": 4, "Y": 1}


def _resolve_model_defaults(cfg_defaults: dict, model_name: str, cls: str) -> dict:
    """Merge flat + per-class defaults for a ``(model, class)`` pair.

    Config shape::

        model_defaults:
          moving_average:
            window: 7                       # flat baseline
            per_class:
              intermittent: {window: 28}    # class-specific override
              lumpy:        {window: 28}
    """
    entry = dict(cfg_defaults.get(model_name, {}) or {})
    per_class = entry.pop("per_class", None) or {}
    entry.update(per_class.get(cls, {}) or {})
    return entry


def _inject_granularity_aware_defaults(
    model_name: str, params: dict, granularity: str,
) -> dict:
    """Fill in params that should follow ``data.granularity`` rather than be
    hardcoded (e.g. ETS ``seasonal_periods`` = 7 at daily, 12 at monthly)."""
    if model_name == "ets" and "seasonal_periods" not in params:
        gran = (granularity or "D").strip().upper()[0]
        params["seasonal_periods"] = _SEASONAL_PERIODS_BY_FREQ.get(gran, 7)
    return params


def _reset_artifact_dirs(cfg: dict) -> int:
    """Delete every artifact file from the previous run so a fresh training
    pass never inherits leftovers — old class pickles whose classes no
    longer exist, prediction CSVs with stale schema columns, outlier bounds
    fit on a different config, etc.

    ``.gitkeep`` sentinels are preserved so the directory scaffolding stays
    intact for source-control and the storage layer. Returns the number of
    files removed (for logging).
    """
    artifacts_dir = Path(cfg["paths"]["artifacts_dir"])
    if not artifacts_dir.exists():
        return 0
    removed = 0
    for p in artifacts_dir.rglob("*"):
        if p.is_file() and p.name != ".gitkeep":
            try:
                p.unlink()
                removed += 1
            except OSError:
                # Best-effort: a file held open by another process (rare
                # — usually only an open log handle) is skipped, not fatal.
                pass
    return removed


def _build_params(
    model_name: str,
    cfg: dict,
    cls: str,
    granularity: str,
    *,
    class_tuned_params: dict | None = None,
) -> dict:
    """Assemble the params dict for ``build_model(model_name, params)``:

      1. Flat / per-class defaults from config.
      2. Random seed from project config.
      3. Granularity-aware inference (ETS seasonal_periods, ...).
      4. For ``two_stage``: propagate any already-tuned LightGBM / RF params
         from the current class into ``regressor_params`` so the inner
         regressor benefits from the same search the top-level model did.
    """
    params = _resolve_model_defaults(cfg["models"].get("model_defaults", {}), model_name, cls)
    params["random_state"] = cfg["project"]["random_seed"]
    params = _inject_granularity_aware_defaults(model_name, params, granularity)

    if model_name == "two_stage" and class_tuned_params:
        inner = (params.get("regressor") or "lightgbm")
        inner_tuned = class_tuned_params.get(inner)
        if inner_tuned:
            params = {**params, "regressor_params": dict(inner_tuned)}
    return params


def _feature_columns(df, group_keys, date_col, target_col):
    """Columns fed to the model: numeric/bool, not group/date/target, not flags,
    and populated (at least one non-NaN value in ``df``).

    The populated-check matters for per-class calls: columns emitted by a
    different class's feature pass appear in ``df`` as all-NaN and must be
    dropped so a class's models don't see unpopulated features."""
    exclude = set(group_keys + [date_col, target_col]) | META_FEATURE_EXCLUDE
    return [
        c for c in df.columns
        if c not in exclude
        and df[c].dtype.kind in "fiub"
        and not df[c].isna().all()
    ]


def _drop_inactive_pairs(df, group_keys, date_col, target_col, max_inactivity_periods, granularity):
    nonzero = df[df[target_col] > 0]
    if nonzero.empty:
        return df.iloc[0:0].copy()
    last_sale = nonzero.groupby(group_keys, as_index=False)[date_col].max().rename(
        columns={date_col: "_last_sale"}
    )
    global_max = df[date_col].max()
    cutoff = global_max - period_offset(granularity, max_inactivity_periods)
    active = last_sale[last_sale["_last_sale"] >= cutoff][group_keys]
    return df.merge(active, on=group_keys, how="inner")


def _filter_models_for_class(enabled, cls, fallback_order):
    cls_models = enabled.get(cls, [])
    out = [m for m in cls_models if is_available(m) or m == "ensemble"]
    return out if out else [m for m in fallback_order if is_available(m)]


def _per_pair_evaluate(
    predictions_by_model,
    group_keys,
    date_col,
    target_col,
    metric,
    *,
    pair_routes: dict[tuple, "Route"] | None = None,
):
    """For each pair, pick the candidate model with the lowest ``metric``.

    If ``pair_routes`` is supplied, the candidate pool per pair is restricted
    to that pair's route-allowed models (and the ensemble component always
    stays eligible — it's not listed in routes but gets composed of already
    allowed models).
    """
    from ..utils.io_utils import ensure_tuple

    pair_best: dict[tuple, tuple[str, float]] = {}
    for m_name, pred_df in predictions_by_model.items():
        for keys, g in pred_df.groupby(group_keys):
            pk = ensure_tuple(keys)
            # Route gate: skip models the route didn't allow for this pair.
            if pair_routes is not None:
                route = pair_routes.get(pk)
                if route is not None and m_name != "ensemble" and m_name not in route.models:
                    continue
            y = g[target_col].values.astype(float)
            p = g["prediction"].values.astype(float)
            m = compute_all(y, p, [metric])
            score = m.get(metric)
            if score is None or not np.isfinite(score):
                score = float("inf")
            if pk not in pair_best or score < pair_best[pk][1]:
                pair_best[pk] = (m_name, score)
    return pair_best


def _build_per_pair_predictions(pair_best, predictions_by_model, group_keys, cls):
    records = []
    for pk, (best_model, _) in pair_best.items():
        pred_df = predictions_by_model[best_model]
        pair_pred = pred_df[pair_mask(pred_df, group_keys, pk)].copy()
        pair_pred["class"] = cls
        pair_pred["best_model"] = best_model
        records.append(pair_pred)
    return pd.concat(records, ignore_index=True) if records else pd.DataFrame()


def run_training(config_path, on_step=None):
    """Run the full training pipeline.

    Args:
        config_path: path to config YAML.
        on_step: optional ``(step_name, status) -> None`` callback fired after
            each major stage completes, so the caller can report live progress.
    """
    _step = on_step or (lambda *_: None)

    cfg = load_config(config_path)
    ensure_dirs(cfg)
    logger = get_logger("train", cfg["paths"]["logs_dir"], cfg["project"]["log_level"])

    # Clear every artifact file from any previous run before writing new ones.
    # Training is the sole producer of these files; stale leftovers (old
    # class pickles, pre-refactor schemas, outdated bounds) would silently
    # corrupt inference. ``.gitkeep`` sentinels in each subdir are preserved.
    removed = _reset_artifact_dirs(cfg)
    if removed:
        logger.info("Cleared %d stale artifact file(s) before fresh training", removed)

    date_col = cfg["data"]["date_col"]
    target_col = cfg["data"]["target_col"]
    group_keys = cfg["data"]["forecast_level"]
    meta_cols = cfg["data"].get("meta_cols", [])
    freq = cfg["data"]["granularity"]
    causal_cols = cfg["data"].get("causal_cols", [])
    activity_flag = cfg["data"].get("activity_flag", False)

    _step("data_collection", "running")
    logger.info("Step 1: load raw data")
    raw_path = cfg["paths"]["raw_data"]
    dtypes = resolve_dtypes(cfg)
    raw = load_raw(raw_path, date_col, dtypes=dtypes)

    logger.info("Step 2: validate raw data")
    validation_report = validate_input(raw, cfg)
    logger.info(
        "Validation: rows=%d bad_dates=%d duplicate_rows=%d future_dates=%d",
        validation_report.n_rows, validation_report.bad_dates,
        validation_report.duplicate_rows, validation_report.future_dates,
    )

    keep_cols = list(dict.fromkeys(
        group_keys + [date_col, target_col] + (meta_cols or []) +
        [cc["col"] for cc in causal_cols if cc["col"] in raw.columns]
    ))
    raw = raw[[c for c in keep_cols if c in raw.columns]].copy()
    _step("data_collection", "completed")

    _step("data_processing", "running")
    logger.info("Step 3: build panel at %s per %s", freq, group_keys)
    agg = build_panel(
        raw, group_keys, date_col, target_col, meta_cols, freq,
        fill_missing=cfg["data"].get("fill_missing_periods", True),
        fill_value=cfg["data"].get("zero_fill_value", 0.0),
        causal_cols=causal_cols,
        activity_flag=activity_flag,
    )

    logger.info("Step 4: drop inactive pairs")
    pairs_before = agg.groupby(group_keys).ngroups
    agg = _drop_inactive_pairs(
        agg, group_keys, date_col, target_col,
        cfg["data"]["max_inactivity_periods"], freq,
    )
    pairs_after = agg.groupby(group_keys).ngroups
    pairs_dropped_inactive = int(pairs_before - pairs_after)
    logger.info(
        "Pairs after inactivity filter: %d (dropped %d)",
        pairs_after, pairs_dropped_inactive,
    )

    if cfg["cleaning"].get("clip_negative_to_zero", True):
        logger.info("Step 5: clip negatives to zero")
        agg = clip_negative_to_zero(agg, target_col)

    lifecycle_cfg = cfg.get("lifecycle", {}) or {}
    if lifecycle_cfg.get("enabled", True):
        logger.info("Step 6: assign lifecycle flags (is_new_launch, is_likely_eol)")
        agg = assign_lifecycle_flags(
            agg, group_keys, target_col, date_col,
            new_launch_periods=int(lifecycle_cfg.get("new_launch_periods", 0)),
            eol_silent_periods=int(lifecycle_cfg.get("eol_silent_periods", 0)),
        )

    anomaly_cfg = (cfg.get("anomaly", {}) or {}).get("suspicious_zeros", {}) or {}
    if anomaly_cfg.get("enabled", True):
        logger.info("Step 7: detect suspicious zero runs")
        agg = detect_suspicious_zeros(
            agg, group_keys, target_col, date_col,
            min_history_periods=int(anomaly_cfg.get("min_history_periods", 60)),
            min_zero_runs=int(anomaly_cfg.get("min_zero_runs", 3)),
            z_threshold=float(anomaly_cfg.get("z_threshold", 3.0)),
            skip_flag_cols=["is_new_launch", "is_likely_eol"],
        )

    test_horizon = cfg["split"]["test_horizon"]
    val_horizon = cfg["split"]["validation_horizon"]
    max_date = agg[date_col].max()
    test_start = max_date - period_offset(freq, test_horizon - 1)
    train_window = agg[agg[date_col] < test_start]
    _step("data_processing", "completed")

    _step("classification", "running")
    logger.info("Step 8: segmentation on train window (date < %s)", test_start.date())
    classification_cfg = cfg["classification"]
    classes_df = classify_dataset(
        train_window, group_keys, target_col,
        float(classification_cfg["adi_intermittent_threshold"]),
        float(classification_cfg["cv2_erratic_threshold"]),
        min_observations=int(classification_cfg.get("min_observations_for_classification", 3)),
    )

    logger.info("Step 9: per-pair outlier treatment (flag-aware, bounds from train window)")
    outlier_cfg = cfg["cleaning"].get("per_pair_outlier", {}) or {}
    outlier_bounds = fit_outlier_bounds(train_window, group_keys, target_col, outlier_cfg)
    agg = apply_outlier_bounds(
        agg, outlier_bounds, group_keys, target_col, outlier_cfg, classes=classes_df,
    )
    # Persist bounds so inference clips the same rows to the same values —
    # otherwise inference's features would systematically differ from the
    # ones the model was trained on.
    if outlier_bounds is not None and not outlier_bounds.empty:
        save_dataframe(
            outlier_bounds,
            os.path.join(cfg["paths"]["models_dir"], "outlier_bounds.csv"),
        )
    _step("classification", "completed")

    _step("feature_engineering", "running")
    logger.info("Step 10: explainability metrics (reuses classes_df; no re-derivation)")
    expl = compute_pair_explainability(
        train_window, group_keys, target_col, classes_df,
        date_col=date_col,
        granularity=freq,
        recent_window_periods=int(cfg["explainability"]["recent_window_periods"]),
    )
    save_dataframe(expl, os.path.join(cfg["paths"]["explainability_dir"], "pair_explainability.csv"))

    logger.info("Step 11: build features (adaptive + target encoding + holidays)")
    horizon = int(cfg.get("inference", {}).get("forecast_horizon", 0))
    full_range_end = agg[date_col].max() + period_offset(freq, horizon)
    full_date_range = build_date_range(agg[date_col].min(), full_range_end, freq)
    feats = build_features(
        agg, group_keys, date_col, target_col, classes_df, cfg["feature_engineering"],
        holiday_cfg=cfg.get("holidays"),
        hijri_cfg=cfg.get("hijri"),
        salary_cycle_cfg=cfg.get("salary_cycle"),
        granularity=freq,
        full_date_range=full_date_range,
        target_encoding_source=train_window,
        forecast_horizon=horizon,
    )

    train, val, test = time_based_split(feats, date_col, test_horizon, val_horizon, granularity=freq)
    logger.info(
        "Split: train=%d val=%d test=%d (test_horizon=%d, val_horizon=%d periods)",
        len(train), len(val), len(test), test_horizon, val_horizon,
    )
    _step("feature_engineering", "completed")

    # ------------------------------------------------------------------
    # Split audit (per-pair row/nonzero counts across train/val/test) +
    # routing signals + Router resolution. Running this *after* the split
    # lets routing rules branch on split-balance signals.
    # ------------------------------------------------------------------
    split_cfg = cfg.get("split_audit") or {}
    split_audit = audit_split(
        classes_df, train, val, test, group_keys, target_col,
    )
    split_summary = summarise_split_audit(
        split_audit,
        min_train_rows=int(split_cfg.get("min_train_rows", 30)),
        min_test_rows=int(split_cfg.get("min_test_rows", 3)),
        min_train_nonzero=int(split_cfg.get("min_train_nonzero", 2)),
    )
    logger.info(
        "Split balance: no_train=%d no_test=%d thin_train=%d thin_test=%d sparse_train=%d",
        split_summary.get("pairs_no_train_rows", 0),
        split_summary.get("pairs_no_test_rows", 0),
        split_summary.get("pairs_below_min_train_rows", 0),
        split_summary.get("pairs_below_min_test_rows", 0),
        split_summary.get("pairs_below_min_train_nonzero", 0),
    )

    # Pair-level lifecycle signals (used by routing) are computed at the
    # train-window edge — same threshold the lifecycle row-flagger uses, so
    # the two views stay in lockstep.
    pair_signals = build_pair_signals(
        agg, group_keys, expl,
        date_col=date_col,
        target_col=target_col,
        new_launch_periods=int(lifecycle_cfg.get("new_launch_periods", 0)),
        eol_silent_periods=int(lifecycle_cfg.get("eol_silent_periods", 0)),
        split_audit_df=split_audit,
    )
    enabled = cfg["models"]["enabled"]
    router = Router.from_config(
        cfg.get("routing"),
        class_menus=enabled,
        known_signals=set(pair_signals.columns),
    )
    pair_routes = router.resolve(pair_signals, group_keys)
    logger.info(
        "Routed %d pairs; %d have custom routes (non-default)",
        len(pair_routes),
        sum(1 for r in pair_routes.values() if r.fired_rules),
    )

    # Data-quality report — built once with everything known at this point:
    # raw stats, validation report, post-processing panel stats, flag counts,
    # and split-balance summary.
    dq_report = build_data_quality_report(
        raw=raw, panel=agg,
        group_keys=group_keys, target_col=target_col, date_col=date_col,
        validation=validation_report,
        raw_path=raw_path,
        pairs_dropped_inactive=pairs_dropped_inactive,
    )
    dq_report.post_processing["split_balance"] = split_summary
    save_json(dq_report.as_dict(), os.path.join(cfg["paths"]["artifacts_dir"], "data_quality.json"))
    logger.info(
        "data_quality report saved: %d pairs, %d flagged rows",
        dq_report.post_processing.get("unique_pairs", 0),
        sum(v for k, v in dq_report.flags.items() if k.endswith("_rows")),
    )

    _step("training", "running")
    fallback = cfg["models"]["fallback_order"]
    selection_metric = cfg["models"]["selection_metric"]
    metrics_names = cfg["evaluation"]["metrics"]
    per_pair_selection = cfg.get("model_selection", {}).get("strategy", "per_class") == "per_pair"
    can_select_on_val = per_pair_selection and not val.empty

    # Per-class feature columns. Each class's models see only the columns
    # actually populated for that class, so feature families defined only in
    # another class's config (e.g. lag_1 for smooth but not intermittent)
    # don't appear as all-NaN.
    feature_cols_by_class: dict[str, list[str]] = {}

    # Route-driven coverage: union of models any pair in the class asked for.
    # Any class menu item that's not in this coverage won't be fit — nothing
    # will ever select it, so the fit would be wasted work.
    pair_to_class = dict(zip(
        map(tuple, classes_df.reset_index()[group_keys].itertuples(index=False, name=None)),
        classes_df["class"].tolist(),
    ))
    route_coverage_by_class = Router.class_model_coverage(pair_routes, pair_to_class)

    # Route-driven HP tuning budget, aggregated at class level. HP tuning in
    # this pipeline fits once per (class, model); to honour per-pair route
    # modifiers we aggregate:
    #   - ``skip_hp_tuning`` fires when the majority of pairs in a class ask
    #     for it (thin-history-dominant classes skip tuning entirely).
    #   - ``hp_trials_multiplier`` uses the per-class median of pair multipliers.
    def _tuning_for_class(cls: str) -> tuple[bool, float]:
        class_routes = [
            pair_routes[pk] for pk, c in pair_to_class.items()
            if c == cls and pk in pair_routes
        ]
        if not class_routes:
            return False, 1.0
        skip_votes = sum(1 for r in class_routes if r.skip_hp_tuning)
        multipliers = sorted(r.hp_trials_multiplier for r in class_routes)
        median_mult = multipliers[len(multipliers) // 2]
        return skip_votes > len(class_routes) / 2, float(median_mult)

    artifacts = {
        "per_class": {},
        "models_path": {},
        "schema": {
            "forecast_level": group_keys,
            "date_col": date_col,
            "target_col": target_col,
            "granularity": freq,
            # Populated below once per-class sets are known.
            "feature_cols": [],
            "per_class_feature_cols": {},
            "meta_cols": [c for c in (meta_cols or []) if c in feats.columns],
            # Thresholds used for this run's classification — persisted so
            # any ``pair_classes.csv`` can be audited against the exact
            # cut-points that produced it.
            "classification_thresholds": {
                "adi_intermittent_threshold": float(classification_cfg["adi_intermittent_threshold"]),
                "cv2_erratic_threshold": float(classification_cfg["cv2_erratic_threshold"]),
                "min_observations_for_classification": int(
                    classification_cfg.get("min_observations_for_classification", 3)
                ),
            },
        },
    }
    test_pred_records = []
    metrics_records = []
    pair_model_lookup = []

    for cls in classes_df["class"].unique():
        logger.info("=== Class: {} ===".format(cls))
        cls_pairs = classes_df[classes_df["class"] == cls].reset_index()[group_keys]
        cls_train = train.merge(cls_pairs, on=group_keys, how="inner")
        cls_val = val.merge(cls_pairs, on=group_keys, how="inner") if not val.empty else val
        cls_test = test.merge(cls_pairs, on=group_keys, how="inner")
        if cls_train.empty or cls_test.empty:
            logger.info("Skipping class {} (empty)".format(cls))
            continue

        cls_feature_cols = _feature_columns(cls_train, group_keys, date_col, target_col)
        feature_cols_by_class[cls] = cls_feature_cols
        logger.info(
            "Class %s: %d feature columns (sample: %s)",
            cls, len(cls_feature_cols), cls_feature_cols[:8],
        )

        models_to_run = _filter_models_for_class(enabled, cls, fallback)
        # Drop any model no pair in this class actually asked for (routing).
        # Ensemble stays eligible whenever at least one pair lists it OR when
        # no routing applied — absence of a non-default route for the class
        # means we should keep today's full behaviour.
        coverage = route_coverage_by_class.get(cls)
        if coverage:
            models_to_run = [m for m in models_to_run if m in coverage or m == "ensemble"]
            if not models_to_run:
                logger.info("Class %s: no models after route filter — using fallback", cls)
                models_to_run = [m for m in fallback if is_available(m)]
        logger.info("Models for {}: {}".format(cls, models_to_run))

        # Class-level tuning decision aggregated from per-pair routes.
        class_skip_tuning, class_hp_mult = _tuning_for_class(cls)

        cls_val_predictions = {}
        cls_test_predictions = {}
        cls_class_metric = {}
        # Remember each model's tuned / resolved params so two_stage (and
        # other wrappers that embed a base model) can reuse them inside
        # the same class iteration.
        class_tuned_params: dict[str, dict] = {}

        for m_name in models_to_run:
            if m_name == "ensemble":
                continue
            try:
                params = _build_params(
                    m_name, cfg, cls, freq,
                    class_tuned_params=class_tuned_params,
                )
                if (cfg["hyperparameter_tuning"]["enabled"] and
                        m_name in cfg["hyperparameter_tuning"]["models_to_tune"] and
                        not cls_val.empty and
                        not class_skip_tuning):
                    scaled_n_trials = max(
                        1, int(cfg["hyperparameter_tuning"]["n_trials"] * class_hp_mult)
                    )
                    logger.info(
                        "Tuning %s for %s (n_trials=%d, class_mult=%.2f)",
                        m_name, cls, scaled_n_trials, class_hp_mult,
                    )
                    tuned = tune_model(
                        m_name, cls_train, cls_val, group_keys, date_col, target_col, cls_feature_cols,
                        n_trials=scaled_n_trials,
                        timeout=int(cfg["hyperparameter_tuning"]["timeout_seconds"]),
                        metric=selection_metric,
                        tuning_cfg=cfg["hyperparameter_tuning"],
                        random_seed=cfg["project"].get("random_seed"),
                    )
                    # Tuned output overrides matching params but keeps
                    # granularity/seed defaults that Optuna doesn't know about.
                    params.update(tuned or {})
                elif class_skip_tuning:
                    logger.info(
                        "Skipping HP tuning for %s %s (route-driven: majority of "
                        "pairs in class requested skip_hp_tuning)",
                        cls, m_name,
                    )

                # Record so two_stage (or any subsequent wrapper) can reuse.
                class_tuned_params[m_name] = dict(params)

                if can_select_on_val:
                    mdl_sel = build_model(m_name, params)
                    mdl_sel.fit(cls_train, group_keys, date_col, target_col, cls_feature_cols)
                    val_preds = mdl_sel.predict(cls_val, group_keys, date_col, target_col, cls_feature_cols)
                    val_merged = cls_val[group_keys + [date_col, target_col]].merge(
                        val_preds, on=group_keys + [date_col], how="left"
                    )
                    val_merged["prediction"] = val_merged["prediction"].fillna(0.0)
                    if "p_demand" not in val_merged.columns:
                        val_merged["p_demand"] = (val_merged["prediction"] > 0).astype(float)
                        val_merged["qty_if_demand"] = val_merged["prediction"]
                    cls_val_predictions[m_name] = val_merged

                fit_data = pd.concat([cls_train, cls_val], ignore_index=True) if not cls_val.empty else cls_train
                mdl_full = build_model(m_name, params)
                mdl_full.fit(fit_data, group_keys, date_col, target_col, cls_feature_cols)
                test_preds = mdl_full.predict(cls_test, group_keys, date_col, target_col, cls_feature_cols)
                test_merged = cls_test[group_keys + [date_col, target_col]].merge(
                    test_preds, on=group_keys + [date_col], how="left"
                )
                test_merged["prediction"] = test_merged["prediction"].fillna(0.0)
                if "p_demand" not in test_merged.columns:
                    test_merged["p_demand"] = (test_merged["prediction"] > 0).astype(float)
                    test_merged["qty_if_demand"] = test_merged["prediction"]
                cls_test_predictions[m_name] = test_merged

                m_metrics = compute_all(test_merged[target_col].values, test_merged["prediction"].values, metrics_names)
                logger.info("{} {} test metrics: {}".format(cls, m_name, m_metrics))
                cls_class_metric[m_name] = m_metrics.get(selection_metric)
                metrics_records.append({"class": cls, "model": m_name, **m_metrics})

                # Production refit: now that test predictions and metrics
                # have been recorded against the held-out (train+val) model,
                # refit on train + val + test so the pickle that inference
                # loads has seen every period up to the training cutoff. The
                # test scores reported to clients stay honest (they came
                # from the held-out model); only the *deployable* model is
                # refit on full data.
                if cfg.get("training", {}).get("refit_on_full_data", True) and not cls_test.empty:
                    fit_data_full = pd.concat([cls_train, cls_val, cls_test], ignore_index=True)
                    try:
                        mdl_prod = build_model(m_name, params)
                        mdl_prod.fit(fit_data_full, group_keys, date_col, target_col, cls_feature_cols)
                        mdl_full = mdl_prod  # this is what gets pickled below
                        logger.info(
                            "Refit %s/%s on full train+val+test (%d rows) for production",
                            cls, m_name, len(fit_data_full),
                        )
                    except Exception as exc:
                        logger.warning(
                            "Production refit failed for %s/%s: %s — keeping train+val fit",
                            cls, m_name, exc,
                        )

                model_path = os.path.join(cfg["paths"]["models_dir"], "{}_{}.pkl".format(cls, m_name))
                try:
                    save_pickle(mdl_full, model_path)
                    artifacts["models_path"]["{}__{}".format(cls, m_name)] = model_path
                except Exception as e:
                    logger.info("Failed to pickle {} {}: {}".format(cls, m_name, e))
            except Exception as e:
                logger.info("Model {} failed for class {}: {}".format(m_name, cls, e))

        if not cls_test_predictions:
            for fb in fallback:
                try:
                    fb_params = _build_params(fb, cfg, cls, freq)
                    mdl = build_model(fb, fb_params)
                    mdl.fit(cls_train, group_keys, date_col, target_col, cls_feature_cols)
                    preds = mdl.predict(cls_test, group_keys, date_col, target_col, cls_feature_cols)
                    merged = cls_test[group_keys + [date_col, target_col]].merge(
                        preds, on=group_keys + [date_col], how="left"
                    )
                    merged["prediction"] = merged["prediction"].fillna(0.0)
                    cls_test_predictions[fb] = merged
                    if can_select_on_val:
                        vp = mdl.predict(cls_val, group_keys, date_col, target_col, cls_feature_cols)
                        vm = cls_val[group_keys + [date_col, target_col]].merge(
                            vp, on=group_keys + [date_col], how="left"
                        )
                        vm["prediction"] = vm["prediction"].fillna(0.0)
                        cls_val_predictions[fb] = vm
                    break
                except Exception:
                    continue

        if "ensemble" in enabled.get(cls, []) and len(cls_test_predictions) >= 2:
            weights = weights_from_metric(cls_class_metric)
            ens_test = weighted_average_ensemble(
                {k: v[group_keys + [date_col, "prediction"]] for k, v in cls_test_predictions.items()}, weights,
            )
            test_merged = cls_test[group_keys + [date_col, target_col]].merge(
                ens_test, on=group_keys + [date_col], how="left"
            )
            test_merged["prediction"] = test_merged["prediction"].fillna(0.0)
            cls_test_predictions["ensemble"] = test_merged
            m_metrics = compute_all(test_merged[target_col].values, test_merged["prediction"].values, metrics_names)
            logger.info("{} ensemble test metrics: {}".format(cls, m_metrics))
            metrics_records.append({"class": cls, "model": "ensemble", **m_metrics})

            if can_select_on_val and len(cls_val_predictions) >= 2:
                ens_val = weighted_average_ensemble(
                    {k: v[group_keys + [date_col, "prediction"]] for k, v in cls_val_predictions.items()}, weights,
                )
                val_merged = cls_val[group_keys + [date_col, target_col]].merge(
                    ens_val, on=group_keys + [date_col], how="left"
                )
                val_merged["prediction"] = val_merged["prediction"].fillna(0.0)
                cls_val_predictions["ensemble"] = val_merged

            save_json(
                {k: float(v) for k, v in (weights or {}).items()},
                os.path.join(cfg["paths"]["models_dir"], "{}_ensemble_weights.json".format(cls)),
            )
            artifacts["models_path"]["{}__ensemble".format(cls)] = "weights"

        # Class-level winner (used as fallback for pairs with no test rows or
        # when per-pair selection isn't active).
        valid_metric = {k: v for k, v in cls_class_metric.items() if v is not None and np.isfinite(v)}
        if valid_metric:
            class_winner = min(valid_metric.items(), key=lambda x: x[1])[0]
        elif cls_test_predictions:
            class_winner = next(iter(cls_test_predictions.keys()))
        else:
            # Last-resort fallback: walk the configured fallback_order and pick
            # the first available. Keep this in lockstep with inference's
            # ``_resolve_fallback_model`` so both layers degrade the same way.
            fallback_chain = list(fallback) + list(cfg["models"].get("fallback_order", []) or [])
            class_winner = next((m for m in fallback_chain if is_available(m)), None)
            if class_winner is None:
                raise RuntimeError(
                    f"No fallback model available for class '{cls}': "
                    f"checked={fallback_chain}. Set models.fallback_order in config."
                )

        selection_source = cls_val_predictions if can_select_on_val else cls_test_predictions
        all_class_pair_tuples = {tuple(row) for row in cls_pairs.itertuples(index=False)}

        if per_pair_selection and len(selection_source) >= 2:
            sel_label = "validation" if can_select_on_val else "test"
            logger.info("Per-pair model selection for {} on {} set".format(cls, sel_label))
            pair_best = _per_pair_evaluate(
                selection_source, group_keys, date_col, target_col, selection_metric,
                pair_routes=pair_routes,
            )
            best_pred = _build_per_pair_predictions(pair_best, cls_test_predictions, group_keys, cls)
            for pk, (bm, sc) in pair_best.items():
                row = {group_keys[i]: pk[i] for i in range(len(group_keys))}
                route = pair_routes.get(pk)
                row.update({
                    "class": cls,
                    "best_model": bm,
                    "score": sc,
                    "route_reason": route.reason if route else "default",
                })
                pair_model_lookup.append(row)
            unscored = all_class_pair_tuples - set(pair_best.keys())
        else:
            logger.info("Class-winner selection for {}: {}".format(cls, class_winner))
            best_pred = (
                cls_test_predictions[class_winner].copy()
                if class_winner in cls_test_predictions else pd.DataFrame()
            )
            if not best_pred.empty:
                best_pred["class"] = cls
                best_pred["best_model"] = class_winner
            for pk in all_class_pair_tuples:
                row = {group_keys[i]: pk[i] for i in range(len(group_keys))}
                route = pair_routes.get(pk)
                row.update({
                    "class": cls,
                    "best_model": class_winner,
                    "route_reason": route.reason if route else "default",
                })
                pair_model_lookup.append(row)
            unscored = set()  # every pair got a row above

        # Unscored pairs (per-pair selection had no test rows for them or the
        # route filtered every candidate out). Assign the class-winner so the
        # lookup always covers every classified pair.
        if unscored:
            logger.warning(
                "Class %s: %d pair(s) had no per-pair score; assigning class-winner '%s'",
                cls, len(unscored), class_winner,
            )
            for pk in unscored:
                row = {group_keys[i]: pk[i] for i in range(len(group_keys))}
                route = pair_routes.get(pk)
                prior_reason = route.reason if route else "default"
                row.update({
                    "class": cls,
                    "best_model": class_winner,
                    "score": None,
                    "route_reason": (
                        f"{prior_reason};class_winner_fallback"
                        if prior_reason != "default" else "class_winner_fallback"
                    ),
                })
                pair_model_lookup.append(row)

        artifacts["per_class"][cls] = {"models_trained": list(cls_test_predictions.keys()), "metrics": cls_class_metric}
        if not best_pred.empty:
            test_pred_records.append(best_pred)

    pi_cfg = cfg.get("evaluation", {}).get("prediction_intervals", {})
    conformal_offsets_df: pd.DataFrame | None = None
    if pi_cfg.get("enabled", False) and is_available("lightgbm_quantile"):
        logger.info("Fitting per-class prediction interval models")
        # Conformal CQR is honest only when calibration data is held out from
        # the quantile fit. Fit on ``train`` only; reserve ``val`` for
        # per-pair offset calibration. The class quantile model loses a
        # little data but the per-pair calibration that becomes possible is
        # a much bigger win for displayed band tightness.
        conformal_cfg = pi_cfg.get("conformal", {}) or {}
        conformal_enabled = bool(conformal_cfg.get("enabled", False)) and not val.empty

        qi_parts: list[pd.DataFrame] = []
        val_calibration_parts: list[pd.DataFrame] = []
        quantiles = pi_cfg.get("quantiles", [0.1, 0.9])

        for cls in classes_df["class"].unique():
            cls_feature_cols = feature_cols_by_class.get(cls)
            if not cls_feature_cols:
                continue  # class never trained — skip quantile fit too
            cls_pairs = classes_df[classes_df["class"] == cls].reset_index()[group_keys]
            if conformal_enabled:
                cls_fit = train.merge(cls_pairs, on=group_keys, how="inner")
            else:
                cls_fit = (
                    pd.concat([train, val], ignore_index=True).merge(cls_pairs, on=group_keys, how="inner")
                    if not val.empty
                    else train.merge(cls_pairs, on=group_keys, how="inner")
                )
            cls_test_qi = test.merge(cls_pairs, on=group_keys, how="inner")
            cls_val_qi = val.merge(cls_pairs, on=group_keys, how="inner") if not val.empty else pd.DataFrame()
            if cls_fit.empty or cls_test_qi.empty:
                continue
            try:
                qi_model = build_model("lightgbm_quantile", {"quantiles": quantiles})
                qi_model.fit(cls_fit, group_keys, date_col, target_col, cls_feature_cols)
                qi_pred = qi_model.predict(cls_test_qi, group_keys, date_col, target_col, cls_feature_cols)
                qi_parts.append(qi_pred)

                # Per-class validation predictions feed conformal calibration.
                # Compute these BEFORE the production refit so calibration sees
                # the held-out (train-only) quantile bands — the principled
                # CQR setup. Production refit comes after.
                if conformal_enabled and not cls_val_qi.empty:
                    val_qi_pred = qi_model.predict(cls_val_qi, group_keys, date_col, target_col, cls_feature_cols)
                    cal = cls_val_qi[group_keys + [date_col, target_col]].merge(
                        val_qi_pred, on=group_keys + [date_col], how="inner",
                    )
                    cal["class"] = cls
                    val_calibration_parts.append(cal)

                # Production refit on full data for the quantile model too,
                # so inference's bands reflect every period the point model
                # has seen. Conformal offsets stay calibrated against the
                # held-out (train-only) bands but are applied as an additive
                # per-pair shift — small approximation, big honesty win.
                if cfg.get("training", {}).get("refit_on_full_data", True) and not cls_test_qi.empty:
                    qi_full = pd.concat(
                        [cls_fit, cls_val_qi, cls_test_qi] if not cls_val_qi.empty else [cls_fit, cls_test_qi],
                        ignore_index=True,
                    )
                    try:
                        qi_prod = build_model("lightgbm_quantile", {"quantiles": quantiles})
                        qi_prod.fit(qi_full, group_keys, date_col, target_col, cls_feature_cols)
                        qi_model = qi_prod
                        logger.info(
                            "Refit quantile %s on full train+val+test (%d rows) for production",
                            cls, len(qi_full),
                        )
                    except Exception as exc:
                        logger.warning(
                            "Production refit failed for quantile %s: %s — keeping calibration fit",
                            cls, exc,
                        )

                save_pickle(qi_model, os.path.join(cfg["paths"]["models_dir"], "quantile_{}.pkl".format(cls)))
            except Exception as e:
                logger.info("Quantile model failed for {}: {}".format(cls, e))
        interval_preds = pd.concat(qi_parts, ignore_index=True) if qi_parts else None

        # Conformal calibration: per-pair offsets from val residuals.
        if conformal_enabled and val_calibration_parts:
            from ..evaluation.conformal import compute_pair_offsets
            cal_df = pd.concat(val_calibration_parts, ignore_index=True)
            target_coverage = float(conformal_cfg.get("target_coverage", 0.80))
            min_samples = int(conformal_cfg.get("min_samples_per_pair", 8))
            conformal_offsets_df = compute_pair_offsets(
                cal_df,
                group_keys=group_keys,
                target_col=target_col,
                target_coverage=target_coverage,
                min_samples_per_pair=min_samples,
            )
            if not conformal_offsets_df.empty:
                save_dataframe(
                    conformal_offsets_df,
                    os.path.join(cfg["paths"]["artifacts_dir"], "conformal_offsets.csv"),
                )
                source_counts = conformal_offsets_df["fallback_source"].value_counts().to_dict()
                logger.info(
                    "Conformal offsets: %d pairs (target_coverage=%.2f, min_samples=%d) sources=%s",
                    len(conformal_offsets_df), target_coverage, min_samples, source_counts,
                )
    else:
        interval_preds = None

    final_test_pred = pd.concat(test_pred_records, ignore_index=True) if test_pred_records else pd.DataFrame()
    if not final_test_pred.empty:
        final_test_pred = final_test_pred.merge(expl, on=group_keys, how="left", suffixes=("", "_expl"))
        if interval_preds is not None:
            qi_cols = [c for c in interval_preds.columns if c.startswith("q_")]
            if qi_cols:
                qi_merged = interval_preds[group_keys + [date_col] + qi_cols]
                final_test_pred = final_test_pred.merge(qi_merged, on=group_keys + [date_col], how="left")
                for qc in qi_cols:
                    final_test_pred[qc] = final_test_pred[qc].clip(lower=0.0)
                # Apply per-pair conformal offsets (if calibrated this run)
                # BEFORE the lo<=hi guard, so the guard catches any residual
                # inversion from a pathological negative offset.
                if conformal_offsets_df is not None and not conformal_offsets_df.empty:
                    from ..evaluation.conformal import apply_pair_offsets, calibrate_to_target_coverage
                    audit_cfg = (conformal_cfg or {}).get("coverage_audit", {}) or {}
                    if bool(audit_cfg.get("enabled", True)) and "class" in final_test_pred.columns:
                        conformal_offsets_df, audit_log = calibrate_to_target_coverage(
                            conformal_offsets_df,
                            final_test_pred,
                            group_keys=group_keys,
                            target_col=target_col,
                            target_coverage=target_coverage,
                            max_iterations=int(audit_cfg.get("max_iterations", 3)),
                            convergence_pp=float(audit_cfg.get("convergence_pp", 2.0)),
                            max_shift_per_iter=float(audit_cfg.get("max_shift_per_iter", 50.0)),
                        )
                        if audit_log:
                            artifacts["conformal_coverage_audit"] = audit_log
                            final = audit_log[-1]
                            logger.info(
                                "Conformal coverage after %d iteration(s): %s",
                                final["iteration"], final["coverage"],
                            )
                            save_dataframe(
                                conformal_offsets_df,
                                os.path.join(cfg["paths"]["artifacts_dir"], "conformal_offsets.csv"),
                            )
                    final_test_pred = apply_pair_offsets(
                        final_test_pred, conformal_offsets_df, group_keys=group_keys,
                    )
                if "q_10" in qi_cols and "q_90" in qi_cols:
                    # Quantile post-processing — TWO deliberate fixes folded
                    # into one min/max sweep:
                    #
                    #   1) Quantile rearrangement (Chernozhukov et al. 2010):
                    #      LightGBM fits each quantile independently, so the
                    #      0.1 and 0.9 models can cross (q_10 > q_90) on
                    #      individual rows. Sorting the pair {q_10, q_90}
                    #      element-wise restores monotonicity at zero
                    #      cost to expected coverage.
                    #
                    #   2) Point-in-band guarantee: the point forecast and
                    #      the quantile bands come from independent models,
                    #      so on rare rows the point can fall outside the
                    #      raw band — incoherent to a customer ("our most
                    #      likely number is outside our likely range?"). We
                    #      widen the band just enough to contain ``pred``
                    #      whenever this happens.
                    #
                    # Effect: q_10 = min(raw_q10, raw_q90, pred);
                    #         q_90 = max(raw_q10, raw_q90, pred).
                    # Coverage is preserved or improved (band only widens),
                    # never narrowed silently.
                    raw_q10 = final_test_pred["q_10"]
                    raw_q90 = final_test_pred["q_90"]
                    pred = final_test_pred["prediction"]
                    final_test_pred["q_10"] = pd.concat([raw_q10, raw_q90, pred], axis=1).min(axis=1)
                    final_test_pred["q_90"] = pd.concat([raw_q10, raw_q90, pred], axis=1).max(axis=1)
    # Quantities ship as physical units -- ceiling at the source so every
    # downstream reader (drift, drawer, /summary) sees ints without
    # having to re-round.
    ceil_int_columns(
        final_test_pred,
        (target_col, "prediction", "qty_if_demand", "q_10", "q_90"),
    )
    save_dataframe(final_test_pred, os.path.join(cfg["paths"]["predictions_dir"], "test_predictions.csv"))

    # Audit snapshot of training-time composite accuracy. UI tiles still
    # recompute from the CSV so this is a self-describing artifact only.
    if not final_test_pred.empty and target_col in final_test_pred.columns and "prediction" in final_test_pred.columns:
        cls_arr = final_test_pred["class"].astype(str).to_numpy() if "class" in final_test_pred.columns else None
        artifacts["composite_accuracy_pct"] = composite_summary(
            final_test_pred[target_col].to_numpy(),
            final_test_pred["prediction"].to_numpy(),
            cls_arr,
        )["accuracy_pct"]

    metrics_df = pd.DataFrame(metrics_records)
    save_dataframe(metrics_df, os.path.join(cfg["paths"]["metrics_dir"], "model_metrics.csv"))

    lookup_df = pd.DataFrame(pair_model_lookup)
    save_dataframe(lookup_df, os.path.join(cfg["paths"]["artifacts_dir"], "pair_model_lookup.csv"))

    # Finalize schema: per-class column lists (used by inference to build the
    # right feature matrix per model) and the union (used by inference as the
    # set to pre-materialize so missing columns surface as zeros).
    artifacts["schema"]["per_class_feature_cols"] = {
        cls: list(cols) for cls, cols in feature_cols_by_class.items()
    }
    union_cols: list[str] = []
    seen: set[str] = set()
    for cols in feature_cols_by_class.values():
        for c in cols:
            if c not in seen:
                seen.add(c)
                union_cols.append(c)
    artifacts["schema"]["feature_cols"] = union_cols

    save_json(artifacts, os.path.join(cfg["paths"]["artifacts_dir"], "training_summary.json"))

    classes_out = classes_df.reset_index()
    save_dataframe(classes_out, os.path.join(cfg["paths"]["explainability_dir"], "pair_classes.csv"))

    _step("training", "completed")
    logger.info("Training pipeline complete")
    return artifacts
