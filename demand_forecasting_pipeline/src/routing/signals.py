"""
Build the per-pair signal frame consumed by the routing engine.

Everything here is sourced from artifacts already produced earlier in the
pipeline (``classify_dataset`` output, ``compute_pair_explainability``
output, and panel-level flags), so this function does no heavy computation
- just alignment and reshaping.

Lifecycle signals (``is_new_launch``, ``is_likely_eol``) are computed as
pair-level state at the *end of the panel* (the train-window end). This is
deliberately different from the row-level columns of the same name in the
panel: those are row annotations used by outlier clipping ("is this row in a
launch phase?"), whereas the routing engine asks the pair-level question
("is this pair currently a launch / EOL pair?"). Aggregating the row flags
with ``any`` would conflate the two and falsely cold-start every pair that
has ever had a launch phase.
"""

from __future__ import annotations

import pandas as pd

# Panel-level row counts that DO aggregate cleanly (counts of flagged rows).
# Boolean lifecycle flags are handled separately below - see module docstring.
_COUNT_REDUCTIONS = {
    "suspicious_zero": "sum",  # becomes suspicious_zero_rows
}


def build_pair_signals(
    panel: pd.DataFrame,
    group_keys: list[str],
    explainability_df: pd.DataFrame,
    *,
    date_col: str,
    target_col: str,
    new_launch_periods: int,
    eol_silent_periods: int,
    split_audit_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Return one row per pair, with every signal a routing rule can match on.

    The frame always contains the ``group_keys`` columns plus any of the
    following that are available:

      - ``class``, ``adi``, ``cv2``
      - ``n_periods``, ``n_nonzero_periods``, ``nonzero_ratio``
      - ``mean_qty``, ``min_qty``, ``max_qty``, ``range_qty``, ``std_qty``,
        ``sum_qty``, ``nonzero_mean``, ``nonzero_median``, ``nonzero_min``,
        ``nonzero_std``, ``avg_gap_days``
      - ``is_new_launch`` (bool) - pair's first non-zero sale lies within
        the last ``new_launch_periods`` panel periods. Pairs with no
        non-zero sales at all are flagged ``True`` (they are by definition
        not yet launched).
      - ``is_likely_eol`` (bool) - pair's silent tail (rows after the last
        non-zero sale) is at least ``eol_silent_periods`` long. Pairs with
        no non-zero sales at all are NOT flagged EOL (a never-launched pair
        is not end-of-life).
      - ``suspicious_zero_rows`` (int)
      - When ``split_audit_df`` is provided:
        ``n_train_rows``, ``n_train_nonzero``,
        ``n_val_rows``, ``n_val_nonzero``,
        ``n_test_rows``, ``n_test_nonzero``

    Missing columns are simply absent from the output - rules that reference
    an absent signal are no-ops for every pair (and logged once by the
    router at startup).
    """
    signals = explainability_df.copy()

    # --- Lifecycle: pair-level "is it currently launching / EOL?" ---------
    if date_col in panel.columns and target_col in panel.columns:
        lifecycle = _pair_lifecycle_state(
            panel,
            group_keys=group_keys,
            date_col=date_col,
            target_col=target_col,
            new_launch_periods=new_launch_periods,
            eol_silent_periods=eol_silent_periods,
        )
        signals = signals.merge(lifecycle, on=group_keys, how="left")
        # Pairs in explainability but absent from panel (shouldn't happen,
        # but be defensive) default to False on both flags.
        signals["is_new_launch"] = signals["is_new_launch"].fillna(False).astype(bool)
        signals["is_likely_eol"] = signals["is_likely_eol"].fillna(False).astype(bool)

    # --- Row-count flags (e.g. suspicious_zero_rows) ----------------------
    present_counts = {col: r for col, r in _COUNT_REDUCTIONS.items() if col in panel.columns}
    for col, reducer in present_counts.items():
        agg = panel.groupby(group_keys, as_index=False)[col].agg(reducer)
        agg = agg.rename(columns={col: f"{col}_rows"})
        signals = signals.merge(agg, on=group_keys, how="left")
        signals[f"{col}_rows"] = signals[f"{col}_rows"].fillna(0).astype(int)

    if split_audit_df is not None and not split_audit_df.empty:
        signals = signals.merge(split_audit_df, on=group_keys, how="left")
        # Pairs present in classes but absent from the audit get zero counts
        # so routing rules that match `n_train_rows: {eq: 0}` fire correctly.
        for col in split_audit_df.columns:
            if col.startswith("n_") and col in signals.columns:
                signals[col] = signals[col].fillna(0).astype(int)

    return signals


def _pair_lifecycle_state(
    panel: pd.DataFrame,
    *,
    group_keys: list[str],
    date_col: str,
    target_col: str,
    new_launch_periods: int,
    eol_silent_periods: int,
) -> pd.DataFrame:
    """Compute pair-level ``is_new_launch`` / ``is_likely_eol`` from panel rows.

    ``is_new_launch`` asks: counting backwards from the panel's max date for
    this pair, is the pair's first non-zero sale within ``new_launch_periods``
    periods? Period count uses panel rows (one row per period at the configured
    granularity), so the result is granularity-correct without needing a freq.

    ``is_likely_eol`` asks: how many panel rows fall strictly after the pair's
    last non-zero sale? If that silent-tail length is >= ``eol_silent_periods``,
    the pair is flagged. Pairs with no non-zero sales at all are not EOL -
    they are pre-launch.
    """
    p = panel[group_keys + [date_col, target_col]].copy()
    p[date_col] = pd.to_datetime(p[date_col], errors="coerce")
    p = p.dropna(subset=[date_col]).sort_values(group_keys + [date_col]).reset_index(drop=True)
    p["_row_idx"] = p.groupby(group_keys).cumcount()

    pair_lengths = p.groupby(group_keys)["_row_idx"].max() + 1  # period count per pair

    nz = p[p[target_col] > 0]
    first_nz_idx = nz.groupby(group_keys)["_row_idx"].min()
    last_nz_idx = nz.groupby(group_keys)["_row_idx"].max()

    out = pair_lengths.to_frame("pair_periods")
    out["first_nz_idx"] = first_nz_idx
    out["last_nz_idx"] = last_nz_idx

    # Periods elapsed since the pair's first non-zero sale (inclusive).
    # Pairs with no sales at all have NaN here -> still treated as "launching".
    out["periods_since_first_sale"] = out["pair_periods"] - out["first_nz_idx"]
    out["is_new_launch"] = (
        out["first_nz_idx"].isna()
        | (out["periods_since_first_sale"] <= new_launch_periods)
    )

    # Silent-tail length = periods after last non-zero sale.
    # NaN last_nz_idx (pair never sold) -> not EOL, just unborn.
    out["silent_tail"] = out["pair_periods"] - out["last_nz_idx"] - 1
    out["is_likely_eol"] = (
        out["last_nz_idx"].notna()
        & (out["silent_tail"] >= eol_silent_periods)
    )

    return out.reset_index()[group_keys + ["is_new_launch", "is_likely_eol"]]
