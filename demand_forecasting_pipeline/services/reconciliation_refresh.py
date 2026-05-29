"""Daily reconciliation refresh: writes carry chain + diagnostics + actual_sold for past+today.

Reads demand_forecast CSV mirror + VW_GET_SALES_DETAILS; computes via enrich_with_load
(same engine as recommend + API lazy fallback); writes yf_sales_transactions via #temp +
MERGE on (trx_date, route_code, item_code). Idempotent, past+today only, atomic per window.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import pyodbc
import structlog

from common.db_pool import FATAL_DB_ERRORS, get_pool, with_db_retry
from common.numeric import safe_float
from common.sql_fragments import NET_SOLD_CASE_SQL, RETURNS_SUBQUERY_BODY_SQL
from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.services.cache_invalidation import (
    invalidate_van_load_cache,
)
from demand_forecasting_pipeline.services.reconciliation.enrich import (
    enrich_with_load,
    forward_fill_closing,
)

logger = logging.getLogger(__name__)


# Lock serialises overlapping refreshes; HELD ONLY across DB write (released before
# cascade HTTP POST so a slow cascade can't queue a second writer indefinitely).
_REFRESH_LOCK = threading.Lock()

# Last successful refresh (UTC); cron short-circuits when within the dedup
# window (settings.reconciliation_cron_dedup_window_seconds). Manual /refresh
# calls bypass via force=True.
_LAST_REFRESH_AT: datetime | None = None


# Engine col -> yf_sales_transactions col; single source of truth for renaming.
_RECON_COL_MAP: dict[str, str] = {
    "opening_stock":               "opening_stock",
    "recommended_load":            "fresh_load",
    "leftover_to_next_day":        "leftover_to_next_day",
    "forecast_corrected":          "forecast_corrected",
    "bias_pct":                    "bias_pct",
    "expected_demand":             "expected_demand",
    "load_lower_bound":            "van_load_lower_bound",
    "load_upper_bound":            "van_load_upper_bound",
    "recent_avg_per_selling_day":  "recent_daily_avg",
    "pattern_floor_applied":       "pattern_floor_applied",
    "pattern_ceiling_applied":     "pattern_ceiling_applied",
    "forecast_below_recent":       "forecast_below_recent",
    "forecast_dormant":            "forecast_dormant",
}

# Natural key; no data_split (transactions are real-world facts).
_KEY_COLS = ("trx_date", "route_code", "item_code")

# Boolean-typed target columns -- staged as BIT in temp table.
_BOOL_COLS = {
    "pattern_floor_applied",
    "pattern_ceiling_applied",
    "forecast_below_recent",
    "forecast_dormant",
}

# Columns that reflect REAL-WORLD truth catching up (late returns, end-of-day stock
# adjustments). These ALWAYS update on re-merge -- including for past dates -- so
# a stale actual_sold from morning gets corrected by evening's late returns.
_REP_SIDE_COLS = frozenset({
    "actual_sold",
    "yaumi_opening_stock",
    "yaumi_fresh_load",
    "yaumi_total_van_load",
    "yaumi_leftover",
})


# Main entry point

def refresh_reconciliation(
    *,
    horizon_days_behind: int = 0,
    settings: Settings | None = None,
    today: datetime | None = None,
    force: bool = True,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Recompute the carry chain + diagnostics for ``[today-behind, today]``
    and UPSERT into yf_sales_transactions.

    Args:
        horizon_days_behind: How many days back from today to refresh.
            ``0`` = today only; daily cron default is 1 (today + yesterday). Wider
            values support backfill-style re-runs across past dates.
        settings: optional override for tests.
        today: optional override for tests.
        force: When ``False``, the call short-circuits if another caller
            already ran a successful refresh within the configured dedup
            window (``settings.reconciliation_cron_dedup_window_seconds``).
            This is the cron-context
            backstop path: the 03:30 demand_forecasting cron used to
            re-run the entire reconciliation 30 minutes after the 03:00
            data_import cascade had already done the same work (~99s of
            wasted compute every night). The "skipped" return now
            collapses that double-fire to a no-op when the primary path
            succeeded; the backstop still works for its real purpose
            (firing when the 03:00 cascade failed silently). Manual
            ``/refresh`` callers and the data_import cascade itself
            pass ``force=True`` so they always perform the work.

    Returns:
        Dict with ``success``, ``rows_updated``, ``window``,
        ``duration_seconds``, and on cascade ``cascade``.
    """
    # Module-level: stamped at the end of every successful refresh,
    # read by the dedup-check below. ``global`` declared up here so
    # the read in the dedup gate and the write at the bottom share the
    # same scope -- Python's parser requires the declaration before
    # the first reference.
    global _LAST_REFRESH_AT

    s = settings or get_settings()
    # Run-id ties every cascade hop (DI/RO/SS) to the same correlation id
    # in the structured logs. Inbound callers (manual /refresh, cron) can
    # pass a pre-existing id to extend an external trace; otherwise we
    # generate a fresh UUID. Bound into structlog so every log emitted by
    # this call surfaces ``run_id`` as a flat JSON field, and forwarded as
    # ``X-Request-ID`` on the three cross-service httpx.post calls below.
    if not run_id:
        run_id = uuid.uuid4().hex
    structlog.contextvars.bind_contextvars(run_id=run_id)

    now = today or datetime.now(UTC)
    today_dt = now.date()
    start = (now - timedelta(days=max(0, int(horizon_days_behind)))).date()
    end = today_dt  # never write future
    window = (str(start), str(end))

    # Cron-context dedup: if a recent successful refresh already wrote
    # rows for this window, the backstop cron has nothing to add. Manual
    # callers (``force=True``) and the data_import cascade always
    # bypass this guard. Read under the lock so two parallel cron ticks
    # can't both decide to skip on a stale snapshot.
    if not force:
        with _REFRESH_LOCK:
            last = _LAST_REFRESH_AT
        if last is not None:
            elapsed = (datetime.now(UTC) - last).total_seconds()
            dedup_window = int(getattr(s, "reconciliation_cron_dedup_window_seconds", 1800))
            if elapsed < dedup_window:
                logger.info(
                    "reconciliation_refresh_skipped_recent: last successful "
                    "refresh was %.0fs ago (<%ds threshold); 03:00 cascade "
                    "already covered this window",
                    elapsed, dedup_window,
                )
                return {
                    "success": True,
                    "skipped": True,
                    "reason": "recent_refresh",
                    "elapsed_seconds": round(elapsed, 1),
                    "window": window,
                }

    if start > end:
        return {"success": False, "error": f"inverted window {window}",
                "window": window}

    t0 = pd.Timestamp.now()

    # Serialise the DB-write phase. A second concurrent caller queues
    # behind the first and observes a no-op MERGE on its turn -- the
    # idempotent contract holds. Lock is released BEFORE the cascade
    # POST below; see ``_REFRESH_LOCK`` docstring above.
    _REFRESH_LOCK.acquire()
    try:
        # 0. Pre-refresh the demand_forecast CSV mirror from the DB.
        #    The engine + BiasService both read this CSV; if a writer
        #    (cron, /pipeline/inference, or a direct DbPusher call)
        #    advanced yf_demand_forecast in the DB without triggering
        #    the data_import cascade, the CSV would be stale and the
        #    bias table would calibrate against the prior model
        #    revision. Pre-refreshing here makes the chain self-healing
        #    -- any path that lands new predictions in the DB is
        #    automatically observed on the next reconciliation tick.
        #    Skipped (logged) when data_import_url isn't configured;
        #    the legacy "trust the upstream cascade" semantics still
        #    apply for those deployments.
        _cascade_data_import_refresh(
            s,
            dataset="demand_forecast",
            lookback_days=int(getattr(s, "forecast_csv_refresh_lookback_days", 60)),
            run_id=run_id,
        )

        # 1. Load demand_forecast CSV (raw model output)
        src = s.shared_data_path(s.demand_forecast_file)
        if not src.exists():
            return {"success": False,
                    "error": f"Mirror not found at {src}; run data_import first",
                    "window": window}
        df = pd.read_csv(src, low_memory=False)
        if df.empty:
            return {"success": True, "rows_updated": 0, "window": window,
                    "duration_seconds": 0.0, "message": "mirror empty"}
        df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce")

        # Stale-forecast guard: refuse to reconcile against a model
        # whose newest forecast row is more than ``forecast_stale_threshold_days``
        # old vs today. A stale model produces stale diagnostics
        # (expected_demand, bands, dormancy flag, bias correction)
        # which then propagate to the UI as if they were current.
        # Failing loudly here is strictly better than silently writing
        # those stale rows under today's trx_date. Threshold is env-
        # overridable via ``DF_FORECAST_STALE_THRESHOLD_DAYS`` so a
        # deliberate weekly-retrain cadence doesn't trip the guard.
        max_forecast_date = df["TrxDate"].max()
        if pd.notna(max_forecast_date):
            stale_days = (pd.Timestamp(now.date()) - max_forecast_date.normalize()).days
            threshold = int(getattr(s, "forecast_stale_threshold_days", 14))
            if stale_days > threshold:
                logger.error(
                    "reconciliation_refresh_refused_stale_forecast: "
                    "newest forecast row %s is %dd old (threshold=%dd). "
                    "Train the model before refreshing.",
                    max_forecast_date.date(), stale_days, threshold,
                )
                return {
                    "success": False,
                    "error_code": "forecast_stale",
                    "error": (f"yf_demand_forecast newest row {max_forecast_date.date()} "
                              f"is {stale_days}d old (threshold {threshold}d); "
                              "refresh aborted to avoid propagating stale diagnostics"),
                    "window": window,
                }

        # 2. Slice to past + today BEFORE enrich. The carry chain needs full
        #    history per (route, item), so we pass everything <= today (not
        #    just the write window). Day-1-of-chain anchors at 0 per the
        #    engine's contract.
        df_past = df[df["TrxDate"].dt.date <= today_dt].copy()
        if df_past.empty:
            return {"success": True, "rows_updated": 0, "window": window,
                    "duration_seconds": 0.0,
                    "message": "no past+today forecast rows in CSV"}

        # 3. Fetch actuals ONCE for the full per-(route, item) chain
        #    range. The engine simulation walks chronologically per pair,
        #    so it needs actuals all the way back to the earliest forecast
        #    date in df_past, not just the write window. Passing these
        #    directly to ``enrich_with_load`` makes the engine simulation
        #    and the persisted ``actual_sold`` column read from the same
        #    VW_GET_SALES_DETAILS snapshot -- closes the truck-cap chain
        #    break that occurred when sales_recent.csv lagged the live
        #    sales view by even a few rows.
        actuals_min_date = df_past["TrxDate"].min().date() if not df_past.empty else start
        engine_actuals_df = _fetch_actual_sold(s, actuals_min_date, end)
        engine_actuals: dict[tuple[str, str, pd.Timestamp], float] = {}
        engine_latest_actual: pd.Timestamp | None = None
        if not engine_actuals_df.empty:
            for r in engine_actuals_df.itertuples(index=False):
                key = (str(r.route_code), str(r.item_code),
                       pd.Timestamp(r.trx_date).normalize())
                engine_actuals[key] = safe_float(r.actual_sold)
            engine_latest_actual = pd.Timestamp(
                engine_actuals_df["trx_date"].max(),
            ).normalize()

        # 4. Engine pass -- same code path the cron has always used,
        #    now with the explicit actuals override above.
        enriched = enrich_with_load(
            df_past,
            predicted_col="Predicted",
            output_col="recommended_load",
            with_diagnostics=True,
            settings=s,
            actuals_override=engine_actuals or None,
            actuals_latest_date=engine_latest_actual,
        )
        missing = [c for c in _RECON_COL_MAP if c not in enriched.columns]
        if missing:
            return {"success": False, "window": window,
                    "error": f"enrich_with_load did not produce columns {missing}"}

        # 4. Dedup to one row per (route, item, date). When both Forecast
        #    and Test splits cover the same cell, Forecast wins -- it's the
        #    fresher production output.
        enriched["_split_pri"] = (
            enriched["DataSplit"].astype(str).str.strip().str.capitalize()
            .map({"Forecast": 0, "Test": 1}).fillna(2)
        )
        enriched = (
            enriched.sort_values("_split_pri")
            .drop_duplicates(["TrxDate", "RouteCode", "ItemCode"], keep="first")
            .drop(columns="_split_pri")
        )

        # 5. Filter to write window (within past + today).
        enriched["TrxDate"] = pd.to_datetime(enriched["TrxDate"], errors="coerce")
        in_window = (
            (enriched["TrxDate"].dt.date >= start)
            & (enriched["TrxDate"].dt.date <= end)
        )
        update_full = enriched[in_window]
        if update_full.empty:
            return {"success": True, "rows_updated": 0, "window": window,
                    "duration_seconds": round((pd.Timestamp.now() - t0).total_seconds(), 2),
                    "message": "no rows in window"}

        # 6. Project to the yf_sales_transactions shape: natural key + mapped
        #    recon columns + computed total_van_load.
        write_df = pd.DataFrame({
            "trx_date":    update_full["TrxDate"].dt.strftime("%Y-%m-%d"),
            "route_code":  update_full["RouteCode"].astype(str),
            "item_code":   update_full["ItemCode"].astype(str),
        })
        for eng_col, db_col in _RECON_COL_MAP.items():
            if db_col in _BOOL_COLS:
                write_df[db_col] = (
                    pd.to_numeric(update_full[eng_col], errors="coerce")
                    .fillna(0).astype(bool).astype(int)
                )
            else:
                write_df[db_col] = (
                    pd.to_numeric(update_full[eng_col], errors="coerce")
                    .fillna(0.0).astype(float)
                )
        # total_van_load = opening_stock + fresh_load (stored derived column).
        write_df["total_van_load"] = (
            write_df["opening_stock"].astype(float)
            + write_df["fresh_load"].astype(float)
        )

        # 7. Reuse the actuals already fetched above for the engine
        #    override. ``engine_actuals_df`` covers [actuals_min_date, end]
        #    which is a superset of the write window [start, end]; filter
        #    down here so the merge only touches rows we are writing.
        # ``how="outer"`` is load-bearing -- not a stylistic choice. With
        # ``how="left"`` the write universe was the forecast frame; any
        # (route, item, date) the rep stocked / sold but the model did
        # not predict for got silently dropped, so the DB's rep-side
        # totals were 5-12% lower than the YaumiLive raw views. The
        # outer merge keeps every observed row from both sides; rep-only
        # rows arrive with NaN in the policy-chain columns -- the upsert
        # layer below converts those to NULL on the wire, and downstream
        # readers that gate on ``(rec + op) > 0`` already skip them.
        if not engine_actuals_df.empty:
            actual_sold_df = engine_actuals_df[
                (engine_actuals_df["trx_date"] >= str(start))
                & (engine_actuals_df["trx_date"] <= str(end))
            ]
            if not actual_sold_df.empty:
                write_df = write_df.merge(
                    actual_sold_df,
                    on=["trx_date", "route_code", "item_code"],
                    how="outer",
                )
            else:
                write_df["actual_sold"] = None
        else:
            write_df["actual_sold"] = None  # column must exist for the MERGE
        # actual_sold remains NULL where no sale exists for that cell.

        # 7b. Pull the rep's (Yaumi) carry chain -- depot allocation + physical
        #     closing stock from YaumiLive (READ-ONLY). Tracked separately from
        #     our policy chain because the depot's allocation is NOT carry-aware
        #     and the rep's leftover reflects damages / returns we don't model.
        yaumi_df = _fetch_yaumi_loading(s, start, end)
        if not yaumi_df.empty:
            write_df = write_df.merge(
                yaumi_df,
                on=["trx_date", "route_code", "item_code"],
                how="outer",
            )
        else:
            for c in (
                "yaumi_opening_stock", "yaumi_fresh_load",
                "yaumi_total_van_load", "yaumi_leftover",
            ):
                write_df[c] = None
        # yaumi_* columns remain NULL where no rep activity exists for that cell.

        # 7c. Outer merges may produce rows whose key columns came in
        #     as pandas NaT/NaN (won't happen on a clean dataset but
        #     guard explicitly so a future data quirk can't write a
        #     malformed key). Drop them with a single visible log line
        #     rather than letting the staging-table INSERT fail late.
        before = len(write_df)
        write_df = write_df.dropna(subset=["trx_date", "route_code", "item_code"])
        dropped = before - len(write_df)
        if dropped:
            logger.warning(
                "reconciliation_refresh dropped %d row(s) with NaN merge keys "
                "after outer merges -- check upstream sales / yaumi feeds",
                dropped,
            )
        # Re-normalise key types so the upsert sees uniform strings even
        # for rows that arrived only from the outer side.
        write_df["trx_date"]   = write_df["trx_date"].astype(str).str[:10]
        write_df["route_code"] = write_df["route_code"].astype(str)
        write_df["item_code"]  = write_df["item_code"].astype(str)

        # 8. UPSERT into yf_sales_transactions
        rows_updated = _upsert_sales_transactions(s, write_df, today_dt=today_dt)
    finally:
        # Release BEFORE the cascade so a slow / retried HTTP round-trip
        # can never block a subsequent caller. The DB MERGE above is the
        # only step that needs serialisation. Note: we do NOT stamp
        # ``_LAST_REFRESH_AT`` here -- the stamp is moved to after the
        # cascades complete so a cascade failure leaves the timestamp
        # stale and the 03:30 backstop cron will rerun the full work.
        # Otherwise: DB write succeeds + cascade fails => stamp is
        # fresh => 03:30 backstop sees "recent refresh" and skips =>
        # CSV mirror lags the DB for up to 24h.
        _REFRESH_LOCK.release()

    # 9. Cascade the new CSV mirror (sales_transactions dataset).
    #    The cascade helper invalidates the VanLoadService cache ONLY
    #    on a fully successful POST -- on failure the cache stays warm
    #    with last-known-good data instead of going cold and forcing
    #    every reader to repopulate from a potentially stale CSV.
    pad = int(getattr(s, "reconciliation_cascade_lookback_pad_days", 2))
    min_days = int(getattr(s, "reconciliation_cascade_lookback_min_days", 7))
    cascade_lookback = max(int(horizon_days_behind) + pad, min_days)
    cascade = _cascade_data_import_refresh(
        s,
        dataset="sales_transactions",
        lookback_days=cascade_lookback,
        run_id=run_id,
    )

    # 10. Cascade -> recommended_order regenerates for each refreshed date.
    recommended_order_cascade = _cascade_recommended_order_generate(s, window=window, run_id=run_id)

    # 11. Cascade -> sales_supervision for EVERY date in the reconciliation
    #     window. The earlier single-date version only invalidated today; a
    #     30-day backfill would refresh past-date recs in the DB but leave
    #     supervision rows orphaned because the invalidate POST only reached
    #     today_dt.
    #
    #     For each date in [start, end], POST /session/internal/invalidate-day.
    #     The endpoint:
    #       (a) drops the in-memory session cache for that date so the next
    #           reconcile tick re-fetches recs from the canonical DB table
    #       (b) repairs yf_supervision_items rows where original_recommended_qty=0
    #           by joining yf_recommended_orders for the true value
    #       (c) recomputes the rolled-up route_performance_score / qty_fulfillment_rate
    #
    #     Best-effort per date: a supervision outage on one date must not
    #     poison the others or the reconciler's own success contract.
    # Per-date gating: a date whose recommended_order regen FAILED has no
    # fresh recs to invalidate against, so skip it. But every date that DID
    # regen successfully should still get its supervision repair. The earlier
    # all-or-nothing gate (one failed rec-order date skipping supervision for
    # the WHOLE window) was the bug -- a 30-day backfill with 1 transient
    # failure left 29 days of supervision orphaned. ``pd`` is module-level;
    # do NOT shadow with a local ``import pandas as pd`` inside this scope
    # (would make pd a local symbol everywhere and break earlier refs).
    if recommended_order_cascade.get("skipped") is True:
        supervision_cascade = {
            "skipped": True,
            "reason": "recommended_order_cascade_skipped",
        }
    else:
        ro_per_date = {entry.get("date"): entry for entry in recommended_order_cascade.get("per_date") or []}
        start_d, end_d = window
        dates = pd.date_range(start=start_d, end=end_d, freq="D").strftime("%Y-%m-%d").tolist()
        per_date_results = []
        all_ok = True
        for d in dates:
            ro_entry = ro_per_date.get(d) or {}
            # If rec-order succeeded for this specific date OR was a no-op
            # skip, the supervision repair still has something to align against.
            if ro_entry.get("success") is True or ro_entry.get("skipped") is True:
                r = _cascade_supervision_invalidate(s, date=d, run_id=run_id)
                per_date_results.append({"date": d, **r})
                if not (r.get("success") is True or r.get("skipped") is True):
                    all_ok = False
            else:
                per_date_results.append({"date": d, "skipped": True,
                                          "reason": "recommended_order_failed_for_this_date"})
                all_ok = False
        supervision_cascade = {
            "skipped": False,
            "success": all_ok,
            "dates_processed": len(dates),
            "per_date": per_date_results,
        }

    # Honest success contract. The DB write landed atomically -- but if
    # the cascade failed, the CSV mirror downstream readers see is now
    # stale relative to the DB. Surfacing success=False here lets HTTP
    # callers retry (or page on it) instead of silently consuming the
    # contradictory mirror. The scheduler logger already inspects
    # ``cascade.success`` separately; this just makes the wire payload
    # tell the same story.
    # Strict ``is True`` semantics so a misbehaving upstream returning a
    # truthy-but-not-bool ``success`` field (string ``"true"``, numeric
    # ``1``) cannot silently mark the cascade as healthy. ``skipped`` is
    # the legitimate "env var not configured" branch -- worth a warning
    # so operators see in the log that downstream consumers are running
    # against an unrefreshed CSV mirror.
    cascade_skipped = cascade.get("skipped") is True
    cascade_ok = cascade_skipped or cascade.get("success") is True
    ro_skipped = recommended_order_cascade.get("skipped") is True
    ro_ok = ro_skipped or recommended_order_cascade.get("success") is True
    if cascade_skipped:
        logger.warning(
            "data_import cascade SKIPPED (reason=%s); downstream CSV mirror "
            "will lag the AIML DB until DF_DATA_IMPORT_URL is configured",
            cascade.get("reason") or "unknown",
        )
    if ro_skipped:
        logger.warning(
            "recommended_order cascade SKIPPED (reason=%s); regenerated "
            "recommendations will lag until RECOMMENDED_ORDER_URL is configured",
            recommended_order_cascade.get("reason") or "unknown",
        )
    full_success = cascade_ok and ro_ok
    payload: dict[str, Any] = {
        "success": full_success,
        "rows_updated": int(rows_updated),
        "window": window,
        "duration_seconds": round((pd.Timestamp.now() - t0).total_seconds(), 2),
        "cascade": cascade,
        "recommended_order_cascade": recommended_order_cascade,
        "supervision_cascade": supervision_cascade,
    }
    if not cascade_ok:
        payload["error"] = (
            f"cascade refresh failed after DB write ({cascade.get('error')}); "
            "CSV mirror is stale -- safe to retry"
        )
        payload["error_code"] = "cascade_failed_retriable"
    elif not ro_ok:
        payload["error"] = (
            f"recommended_order cascade failed after CSV cascade "
            f"({recommended_order_cascade.get('error')}); recommendations "
            f"may be stale for some dates in the window -- safe to retry"
        )
        payload["error_code"] = "recommended_order_cascade_failed_retriable"
    # Stamp _LAST_REFRESH_AT ONLY on a fully-successful run (DB write
    # AND both cascades). Cascade failure leaves the stamp stale so
    # the 03:30 backstop cron re-runs the full work next time -- the
    # CSV mirror cannot silently lag the DB for a full day after a
    # transient cascade outage. Stamp under the lock so a concurrent
    # cron tick sees the value atomically.
    if full_success:
        # ``global`` already declared at the top of the function.
        with _REFRESH_LOCK:
            _LAST_REFRESH_AT = datetime.now(UTC)  # noqa: F841
    return payload


def _cron_propagation_headers(run_id: str | None) -> dict[str, str]:
    """Headers every cross-service cascade call must carry so the receiving
    backend's ``common.api_middleware`` binds the same ``request_id`` into
    its structured logs. The cron's run_id therefore correlates every hop
    in the chain (DF -> data_import -> recommended_order -> supervision)
    end-to-end."""
    if not run_id:
        return {}
    return {"X-Request-ID": run_id}


def _cascade_recommended_order_generate(
    s: Settings, *, window: tuple[str, str], run_id: str | None = None,
) -> dict[str, Any]:
    """POST recommended_order /generate?force=true per date in window."""
    url_base = (getattr(s, "recommended_order_url", "") or "").strip().rstrip("/")
    if not url_base:
        return {"skipped": True, "reason": "RECOMMENDED_ORDER_URL not set"}

    import httpx
    start_d, end_d = window
    dates = pd.date_range(start=start_d, end=end_d, freq="D").strftime("%Y-%m-%d").tolist()
    timeout = float(
        getattr(s, "recommended_order_generate_timeout_seconds", 600.0)
    )

    def _post_once(d: str) -> dict[str, Any]:
        with httpx.Client(timeout=timeout) as client:
            # Re-entrancy header: we ARE the reconciliation_refresh that
            # populates yf_sales_transactions; if RO's auto-heal called back
            # into reconciliation/refresh while we hold its locks, the
            # cascade would deadlock. RO reads this header and short-circuits
            # ``_ensure_carry_chain_present`` -- it trusts our state.
            resp = client.post(
                f"{url_base}/api/v1/recommended-order/generate",
                json={"date": d, "force": True},
                headers={
                    "X-Skip-Carry-Chain-Heal": "1",
                    **_cron_propagation_headers(run_id),
                },
            )
            resp.raise_for_status()
            body = resp.json()
        return {
            "date": d,
            "success": bool(body.get("success")),
            "routes_processed": int(body.get("routes_processed") or 0),
            "total_records": int(body.get("total_records") or 0),
            "duration_seconds": float(body.get("duration_seconds") or 0.0),
        }

    results: list[dict[str, Any]] = []
    overall_success = True
    for d in dates:
        # One retry on transient HTTP / 5xx failure -- mirrors
        # _cascade_data_import_refresh's pattern so the cron pair is
        # resilient to a brief recommended_order blip without leaving
        # downstream state stale.
        try:
            row = _post_once(d)
        except Exception as exc:
            logger.warning(
                "recommended_order cascade attempt 1 failed for date=%s: %s; "
                "retrying in %.1fs",
                d, exc, _CASCADE_RETRY_DELAY_SECONDS,
            )
            time.sleep(_CASCADE_RETRY_DELAY_SECONDS)
            try:
                row = _post_once(d)
            except Exception as exc2:
                logger.error(
                    "recommended_order cascade failed after retry for date=%s: %s",
                    d, exc2,
                )
                row = {"date": d, "success": False, "error": str(exc2)}
        results.append(row)
        if not row.get("success"):
            overall_success = False

    return {
        "skipped": False,
        "success": overall_success,
        "dates_processed": len(dates),
        "per_date": results,
    }


def _cascade_supervision_invalidate(
    s: Settings, *, date: str, run_id: str | None = None,
) -> dict[str, Any]:
    """POST sales_supervision /session/internal/invalidate-day so cached
    sessions drop their stale rec snapshot. Best-effort: a supervision
    outage must not poison the reconciler's own success contract -- by
    the time this fires, DB + downstream CSVs are already consistent.
    """
    url_base = (getattr(s, "sales_supervision_url", "") or "").strip().rstrip("/")
    if not url_base:
        return {"skipped": True, "reason": "DF_SALES_SUPERVISION_URL not set"}

    import httpx
    timeout = float(getattr(s, "sales_supervision_cascade_timeout_seconds", 10.0))
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{url_base}/api/v1/supervision/session/internal/invalidate-day",
                params={"date": date},
                headers=_cron_propagation_headers(run_id),
            )
            resp.raise_for_status()
            payload = resp.json()
        return {
            "skipped": False,
            "success": bool(payload.get("success")),
            "auto_visit_dropped": payload.get("auto_visit_dropped"),
            "registry_dropped": payload.get("registry_dropped"),
        }
    except Exception as exc:
        logger.warning(
            "supervision invalidate-day cascade failed for date=%s: %s; "
            "supervisor sessions will pick up fresh recs after TTL expiry",
            date, exc,
        )
        return {"skipped": False, "success": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# Actual sold lookup (read-only YaumiLive)
# ---------------------------------------------------------------------------

def _fetch_actual_sold(
    s: Settings,
    start: Any,
    end: Any,
) -> pd.DataFrame:
    """Pull net actual_sold per (route, item, date) from
    VW_GET_SALES_DETAILS for the [start, end] window. YaumiLive is
    READ-ONLY -- this is a query, never a write.

    Return-netting (mirrors ``data_import.core.queries.sales_recent`` --
    the canonical netting pattern; keep the two in lockstep):

      * Each sales-invoice line ``(TrxCode, ItemCode)`` LEFT JOINs the
        sum of any return rows pointing back via
        ``ReturnItem_InvoiceRef``. The subtraction lands on the
        ORIGINAL invoice date (lag-aware), which is the right
        semantic: a return on day +5 reduces day-0's demand, not
        day +5's.
      * Line-level clamp at 0 so an over-return (data error) cannot
        push a line negative and mask other lines in the same group.
      * Returns subquery carries the SAME route + date predicates as
        the outer query, so it never scans rows the outer wouldn't
        have read anyway -- bounded subset, not a second full pass.
      * Returns can only post-date their original invoice; a return
        outside the window can only point to an invoice that is also
        outside the window (which the outer query already excludes).
        So bounding the subquery by the same window loses no
        in-window netting.

    Raw ``pyodbc.connect`` here is intentional (vs the AIML pool):
    one-shot live cut-through fired once per cron tick, so a pool
    would never see warm reuse and only adds shutdown bookkeeping."""
    empty = pd.DataFrame(columns=["trx_date", "route_code", "item_code", "actual_sold"])
    # Gate on live DB configuration. Without creds the column stays NULL.
    if not getattr(s, "live_db_configured", False):
        return empty

    # Route-filter at the source. ``live_route_codes`` is the fleet our
    # downstream consumers actually read -- the CSV mirror, every
    # yf_sales_transactions SELECT in this repo, and every API surface
    # already restrict to ``route_code IN (registry)``. Without this
    # filter the YaumiLive scan returns ~348 routes and the MERGE
    # writes all of them, inflating the daily transaction ~28x and
    # widening the lock window for rows no consumer reads.
    # Empty config preserves the legacy "no filter" behavior so a
    # misconfiguration cannot silently drop rows; a populated registry
    # is the production path.
    routes = list(getattr(s, "live_route_codes", []) or [])
    route_ph = ",".join("?" for _ in routes) if routes else ""

    # Outer params (sales invoice WHERE) then inner subquery params
    # (return TrxTypes + route filter + date window). Order matters:
    # the subquery is embedded in the FROM/LEFT JOIN, which SQL Server
    # binds left-to-right with the outer's params.
    outer_params: list = [
        s.sales_item_type,
        s.sales_invoice_trx_type,
    ]
    inner_params: list = [
        s.bad_return_trx_type,
        s.good_return_trx_type,
    ]
    if routes:
        outer_params.extend(str(r) for r in routes)
        inner_params.extend(str(r) for r in routes)
    outer_params.extend([str(start), str(end)])
    inner_params.extend([str(start), str(end)])

    route_clause_outer = f"AND s.RouteCode IN ({route_ph})" if routes else ""
    route_clause_inner = f"AND r.RouteCode IN ({route_ph})" if routes else ""

    # Both the netting CASE and the returns subquery come from the
    # shared ``common.sql_fragments`` module so this query and
    # ``data_import.core.queries.sales_recent`` cannot drift on the
    # netting semantics. Any future change to the formula propagates
    # to both call sites in lockstep.
    returns_subquery = RETURNS_SUBQUERY_BODY_SQL.format(
        view=s.live_sales_view,
        route_clause=route_clause_inner,
        date_clause="AND CAST(r.TrxDate AS DATE) BETWEEN ? AND ?",
    )
    sql = f"""
    SELECT
        CAST(s.TrxDate AS DATE) AS trx_date,
        s.RouteCode             AS route_code,
        s.ItemCode              AS item_code,
        SUM({NET_SOLD_CASE_SQL}) AS actual_sold
    FROM {s.live_sales_view} s WITH (NOLOCK)
    LEFT JOIN ({returns_subquery}) rj
        ON s.TrxCode  = rj.InvoiceRef
       AND s.ItemCode = rj.ItemCode
    WHERE s.ItemType = ?
      AND s.TrxType  = ?
      {route_clause_outer}
      AND CAST(s.TrxDate AS DATE) BETWEEN ? AND ?
    GROUP BY CAST(s.TrxDate AS DATE), s.RouteCode, s.ItemCode;
    """
    params = inner_params + outer_params
    try:
        with pyodbc.connect(s.live_connection_string(), autocommit=False) as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        df = pd.DataFrame.from_records(rows, columns=cols)
        if df.empty:
            return empty
        df["trx_date"] = pd.to_datetime(df["trx_date"]).dt.strftime("%Y-%m-%d")
        df["route_code"] = df["route_code"].astype(str)
        df["item_code"] = df["item_code"].astype(str)
        df["actual_sold"] = pd.to_numeric(df["actual_sold"], errors="coerce")
        return df
    except Exception as exc:
        logger.warning(
            "reconciliation refresh: actual_sold lookup failed -- "
            "column will be NULL: %s", exc,
        )
        return empty


# ---------------------------------------------------------------------------
# Yaumi (rep) carry chain -- depot allocation + physical closing stock
# ---------------------------------------------------------------------------

def _fetch_yaumi_loading(
    s: Settings,
    start: Any,
    end: Any,
) -> pd.DataFrame:
    """Pull the rep's actual van loading from YaumiLive (READ-ONLY).

    Sources:
      * ``VW_GET_LOAD_ALLOCATION_DETAILS`` -- depot's fresh allocation per
        (route, item, date). NOT carry-aware; the depot ships a standard
        quantity regardless of what's still on the truck.
      * ``VW_GET_CLOSING_STOCK`` -- rep's physical end-of-day stock per
        (route, item, date). Reflects reality including damages / returns.

    Derivations per (route, item) on [start, end]:
        yaumi_opening_stock[d]  = closing[d-1]   (forward-filled across gaps)
        yaumi_fresh_load[d]     = AllocatedPC[d]
        yaumi_total_van_load[d] = yaumi_opening + yaumi_fresh
        yaumi_leftover[d]       = closing[d]

    The forward fill follows the same convention the live van-load
    surface (``page_views`` / ``forward_fill_closing``) uses: on non-trip
    days the rep's closing stays the same as the prior trip.

    Returns an empty frame on connection / parse failure; the caller
    surfaces yaumi_* as NULL (write_df assignment), so a transient
    YaumiLive blip leaves the row's policy chain intact and only the
    rep-side columns NULL for that cron pass.
    """
    empty_cols = [
        "trx_date", "route_code", "item_code",
        "yaumi_opening_stock", "yaumi_fresh_load",
        "yaumi_total_van_load", "yaumi_leftover",
    ]
    if not getattr(s, "live_db_configured", False):
        return pd.DataFrame(columns=empty_cols)

    # Pull closing for [start - lookback, end] so closing[d-1] is reachable
    # for every d in [start, end] even when the rep skipped trips in the
    # immediate run-up to ``start``.
    lookback_days = int(getattr(s, "opening_stock_lookback_days", 30))
    closing_start = (
        pd.Timestamp(start) - pd.Timedelta(days=lookback_days + 1)
    ).date()

    # Route-filter both rep-side queries at the source, same reasoning
    # as ``_fetch_actual_sold``: every consumer of yf_sales_transactions
    # filters on the registry, so pulling closing/allocation for the
    # other ~336 routes only inflates the upstream view scan and the
    # downstream MERGE without changing what anyone reads.
    routes = list(getattr(s, "live_route_codes", []) or [])
    route_clause = ""
    if routes:
        route_clause = f"AND RouteCode IN ({','.join(['?'] * len(routes))})"

    closing_sql = f"""
    SELECT
        CAST(TrxDate AS DATE)   AS trx_date,
        RouteCode               AS route_code,
        ItemCode                AS item_code,
        SUM(ClosingQty)         AS closing
    FROM [YaumiLive].[dbo].[VW_GET_CLOSING_STOCK] WITH (NOLOCK)
    WHERE CAST(TrxDate AS DATE) BETWEEN ? AND ?
      {route_clause}
    GROUP BY CAST(TrxDate AS DATE), RouteCode, ItemCode;
    """
    alloc_sql = f"""
    SELECT
        CAST(MovementDate AS DATE)         AS trx_date,
        RouteCode                          AS route_code,
        ItemCode                           AS item_code,
        SUM(AllocatedQuantityInPC)         AS alloc
    FROM [YaumiLive].[dbo].[VW_GET_LOAD_ALLOCATION_DETAILS] WITH (NOLOCK)
    WHERE CAST(MovementDate AS DATE) BETWEEN ? AND ?
      {route_clause}
    GROUP BY CAST(MovementDate AS DATE), RouteCode, ItemCode;
    """

    closing_params: list = [str(closing_start), str(end)]
    alloc_params: list = [str(start), str(end)]
    if routes:
        closing_params.extend(str(r) for r in routes)
        alloc_params.extend(str(r) for r in routes)

    try:
        with pyodbc.connect(s.live_connection_string(), autocommit=False) as conn:
            cur = conn.cursor()
            cur.execute(closing_sql, closing_params)
            cols = [d[0] for d in cur.description]
            closing_df = pd.DataFrame.from_records(cur.fetchall(), columns=cols)
            cur.execute(alloc_sql, alloc_params)
            cols = [d[0] for d in cur.description]
            alloc_df = pd.DataFrame.from_records(cur.fetchall(), columns=cols)
    except Exception as exc:
        logger.warning("yaumi loading fetch failed -- yaumi_* will be NULL: %s", exc)
        return pd.DataFrame(columns=empty_cols)

    if closing_df.empty and alloc_df.empty:
        return pd.DataFrame(columns=empty_cols)

    def _norm(df: pd.DataFrame, qty_col: str) -> pd.DataFrame:
        if df.empty:
            return df
        df = df.copy()
        df["trx_date"]   = pd.to_datetime(df["trx_date"], errors="coerce").dt.normalize()
        df["route_code"] = df["route_code"].astype(str)
        df["item_code"]  = df["item_code"].astype(str)
        df[qty_col]      = pd.to_numeric(df[qty_col], errors="coerce").fillna(0.0)
        return df.dropna(subset=["trx_date"])

    closing_df = _norm(closing_df, "closing")
    alloc_df   = _norm(alloc_df,   "alloc")

    # Forward-fill closing across non-trip days within the lookback window.
    # forward_fill_closing expects PascalCase column names.
    if not closing_df.empty:
        cf = closing_df.rename(columns={
            "route_code": "RouteCode", "item_code": "ItemCode",
            "trx_date":   "TrxDate",   "closing":   "ClosingQty",
        })
        cf = forward_fill_closing(cf, lookback_days)
        closing_df = cf.rename(columns={
            "RouteCode": "route_code", "ItemCode": "item_code",
            "TrxDate":   "trx_date",   "ClosingQty": "closing",
        })

    target_start = pd.Timestamp(start).normalize()
    target_end   = pd.Timestamp(end).normalize()
    on_keys = ["trx_date", "route_code", "item_code"]

    # yaumi_leftover[d] = closing[d]
    leftover_today = closing_df[
        (closing_df["trx_date"] >= target_start)
        & (closing_df["trx_date"] <= target_end)
    ].rename(columns={"closing": "yaumi_leftover"})[on_keys + ["yaumi_leftover"]]

    # yaumi_opening_stock[d] = closing[d-1] (shift the closing frame forward
    # by one day before clipping to the target window).
    opening_today = closing_df.copy()
    opening_today["trx_date"] = opening_today["trx_date"] + pd.Timedelta(days=1)
    opening_today = opening_today[
        (opening_today["trx_date"] >= target_start)
        & (opening_today["trx_date"] <= target_end)
    ].rename(columns={"closing": "yaumi_opening_stock"})[on_keys + ["yaumi_opening_stock"]]

    # yaumi_fresh_load[d] = AllocatedPC[d]
    fresh_today = alloc_df[
        (alloc_df["trx_date"] >= target_start)
        & (alloc_df["trx_date"] <= target_end)
    ].rename(columns={"alloc": "yaumi_fresh_load"})[on_keys + ["yaumi_fresh_load"]]

    # Outer-merge so a (route, item, date) row surfaces whenever ANY of the
    # three signals exists for it.
    merged = leftover_today.merge(opening_today, on=on_keys, how="outer")
    merged = merged.merge(fresh_today, on=on_keys, how="outer")
    if merged.empty:
        return pd.DataFrame(columns=empty_cols)

    merged["yaumi_opening_stock"] = merged["yaumi_opening_stock"].fillna(0.0)
    merged["yaumi_fresh_load"]    = merged["yaumi_fresh_load"].fillna(0.0)
    merged["yaumi_leftover"]      = merged["yaumi_leftover"].fillna(0.0)
    merged["yaumi_total_van_load"] = (
        merged["yaumi_opening_stock"] + merged["yaumi_fresh_load"]
    )
    merged["trx_date"] = pd.to_datetime(merged["trx_date"]).dt.strftime("%Y-%m-%d")
    return merged[on_keys + [
        "yaumi_opening_stock", "yaumi_fresh_load",
        "yaumi_total_van_load", "yaumi_leftover",
    ]]


# ---------------------------------------------------------------------------
# UPSERT into yf_sales_transactions
# ---------------------------------------------------------------------------

@with_db_retry
def _upsert_sales_transactions(
    s: Settings, write_df: pd.DataFrame, *, today_dt: Any,
) -> int:
    """Stage -> MERGE write_df into yf_sales_transactions.

    UPSERT semantics: WHEN MATCHED THEN UPDATE, WHEN NOT MATCHED THEN
    INSERT. The cron's first call of the day creates today's rows; the
    second call updates them with fresh actuals.

    Wrapped in ``with_db_retry`` so a SQL Server deadlock-victim 1205
    on the MERGE (collision with a concurrent supervision reader
    holding a key-range lock on yf_sales_transactions) backs off
    50ms / 100ms / 200ms / 400ms / 800ms instead of failing the entire
    refresh. Fatal errors (bad SQL, schema mismatch) still raise
    immediately -- they wouldn't get better by retrying.
    """
    if write_df.empty:
        return 0

    target_cols = [
        # Our policy chain
        "opening_stock", "fresh_load", "total_van_load", "leftover_to_next_day",
        # Rep (Yaumi) chain -- observed allocation + closing stock
        "yaumi_opening_stock", "yaumi_fresh_load", "yaumi_total_van_load", "yaumi_leftover",
        # Reality
        "actual_sold",
        # Engine math
        "bias_pct", "forecast_corrected", "expected_demand",
        "van_load_lower_bound", "van_load_upper_bound",
        # Envelope diagnostics
        "recent_daily_avg",
        "pattern_floor_applied", "pattern_ceiling_applied", "forecast_below_recent",
        # Dormancy guard -- zero expected_demand for cold (route, item) pairs.
        "forecast_dormant",
    ]
    # Make sure every target column exists in write_df (default NULL).
    for c in target_cols:
        if c not in write_df.columns:
            write_df[c] = None

    # Build the records tuple in the exact order of staging columns.
    #
    # Vectorised build: ``iterrows()`` over a tens-of-thousands-of-rows
    # frame INSIDE a HOLDLOCK+UPDLOCK transaction held the SERIALIZABLE
    # range lock open for minutes; the column-wise pass below builds the
    # same row tuples in NumPy and is typically 50-100x faster.
    #
    # CRITICAL: pyodbc's ``fast_executemany`` rejects numpy scalar dtypes
    # (``numpy.int64`` / ``numpy.float64`` / ``numpy.str_``) with
    # ``ProgrammingError: Unknown object type ... during describe``. Every
    # column is materialised as a Python list via ``.tolist()`` (numpy
    # downcasts to native ints/floats) or an explicit list comprehension
    # (for the str + nan-aware float cases) so the driver only sees
    # ``str | int | float | None``.
    all_cols = list(_KEY_COLS) + target_cols
    if write_df.empty:
        records: list[tuple] = []
    else:
        col_lists: list[list] = []
        for c in all_cols:
            if c in write_df.columns:
                series = write_df[c]
            else:
                series = pd.Series([None] * len(write_df), index=write_df.index)

            if c in _BOOL_COLS:
                # BIT NULL: 0/1 ints, missing -> 0.
                col_lists.append(
                    pd.to_numeric(series, errors="coerce")
                      .fillna(0)
                      .astype(int)
                      .tolist()
                )
            elif c in _KEY_COLS:
                # NVARCHAR NOT NULL: native Python str.
                col_lists.append([str(v) for v in series])
            else:
                # FLOAT NULL: native float for present values, ``None``
                # for missing. pyodbc's NULL handling for mixed-type
                # columns requires Python ``None``, not ``numpy.nan``.
                numeric = pd.to_numeric(series, errors="coerce")
                col_lists.append([
                    None if pd.isna(v) else float(v) for v in numeric
                ])
        # zip(*lists) emits one tuple per row, matching the previous shape.
        records = list(zip(*col_lists, strict=False))

    pool = get_pool(
        s.db.connection_string(),
        max_connections=max(s.db.retry_attempts + 1, 4),
        connect_timeout=s.db.connection_timeout,
        query_timeout=s.db.query_timeout,
        autocommit=False,
    )

    # Build the staging-table DDL dynamically -- key cols + target cols
    # with their right SQL types.
    key_ddl = (
        "[trx_date]    DATE         NOT NULL,\n        "
        "[route_code]  NVARCHAR(50) NOT NULL,\n        "
        "[item_code]   NVARCHAR(50) NOT NULL"
    )
    target_ddl_parts = []
    for c in target_cols:
        if c in _BOOL_COLS:
            target_ddl_parts.append(f"[{c}] BIT NULL")
        else:
            target_ddl_parts.append(f"[{c}] FLOAT NULL")
    target_ddl = ",\n        ".join(target_ddl_parts)

    col_list = ", ".join(f"[{c}]" for c in all_cols)
    placeholders = ", ".join("?" for _ in all_cols)
    src_cols = ", ".join(f"S.[{c}]" for c in all_cols)
    on_clause = " AND ".join(f"T.[{k}] = S.[{k}]" for k in _KEY_COLS)

    # MODEL-SIDE columns are frozen for past dates when the freeze flag is on:
    # a retrain would otherwise silently rewrite yesterday's "what we recommended"
    # history, breaking past-performance audit trails. Rep-side cols (actual_sold,
    # yaumi_*) always update so late returns + closing-stock corrections flow in.
    #
    # Threshold is Python's ``today_dt`` (computed under refresh_reconciliation's
    # UTC clock), NOT SQL Server's GETDATE() -- if the DB instance lives in a
    # non-UTC timezone, GETDATE() can be one day ahead of the trx_date values
    # Python writes, which would incorrectly freeze today's row at the day boundary.
    # The string is server-controlled (date arithmetic, never user input), safe
    # to inline.
    #
    # Wraps in COALESCE-style fallback: if the existing target row has NULL in a
    # model col (e.g. very first MERGE landed during a partial engine output),
    # take the new value rather than perpetually freezing NULL.
    freeze_past = bool(getattr(s, "reconciliation_freeze_past_model_cols", True))
    # Canonical YYYY-MM-DD via strftime (str()[:10] worked but was a fragile
    # representation contract -- date/datetime/Timestamp all differ).
    today_iso = today_dt.strftime("%Y-%m-%d")
    set_parts: list[str] = []
    for c in target_cols:
        if (not freeze_past) or (c in _REP_SIDE_COLS):
            set_parts.append(f"T.[{c}] = S.[{c}]")
        else:
            set_parts.append(
                f"T.[{c}] = CASE "
                f"WHEN S.[trx_date] >= CAST('{today_iso}' AS DATE) THEN S.[{c}] "
                f"WHEN T.[{c}] IS NULL THEN S.[{c}] "
                f"ELSE T.[{c}] END"
            )
    set_parts.append("T.[updated_at] = GETDATE()")
    set_clause = ", ".join(set_parts)

    with pool.acquire() as conn:
        cur = conn.cursor()
        try:
            cur.fast_executemany = True
            # SERIALIZABLE + HOLDLOCK/UPDLOCK matches db_pusher's pattern.
            # Without these, the MERGE runs at READ COMMITTED and a
            # concurrent INSERT can sneak between MATCH and INSERT --
            # ``@with_db_retry`` catches the deadlock today, but
            # eliminating the race is strictly better than retrying it.
            # Range locks are released at commit; the in-process
            # ``_REFRESH_LOCK`` keeps the window short.
            cur.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;")
            cur.execute(f"""
                CREATE TABLE #sales_stage (
                    {key_ddl},
                    {target_ddl}
                );
            """)
            cur.executemany(
                f"INSERT INTO #sales_stage ({col_list}) VALUES ({placeholders})",
                records,
            )
            cur.execute(f"""
                MERGE {s.sales_transactions_table} WITH (HOLDLOCK, UPDLOCK) AS T
                USING (
                    SELECT *, GETDATE() AS updated_at FROM #sales_stage
                ) AS S
                  ON {on_clause}
                WHEN MATCHED THEN
                    UPDATE SET {set_clause}
                WHEN NOT MATCHED THEN
                    INSERT ({col_list}, [updated_at])
                    VALUES ({src_cols}, GETDATE());
            """)
            rows = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.commit()
            return int(rows)
        except FATAL_DB_ERRORS as exc:
            conn.rollback()
            logger.error("sales_transactions UPSERT: fatal DB error: %s", exc)
            raise
        except Exception:
            try:
                conn.rollback()
            except Exception as rollback_exc:
                # A rollback that itself raises is a serious signal --
                # the connection is in an undefined state. Log at
                # ERROR so the orphaned-connection event is visible;
                # original write error still re-raised below.
                logger.error(
                    "sales_transactions UPSERT rollback FAILED "
                    "(connection state undefined): %s", rollback_exc,
                )
            raise
        finally:
            # ``DROP TABLE IF EXISTS`` (SQL Server 2016+) is the
            # cleanup that survives a poisoned cursor. The previous
            # ``IF OBJECT_ID(...) IS NOT NULL DROP TABLE...`` runs as
            # a regular statement and re-raises through the cursor
            # if the connection is already in a broken state -- the
            # bare ``except: pass`` then leaves the temp table
            # orphaned in tempdb. Under connection pooling the orphan
            # accumulates across cron ticks because the connection is
            # re-used. ``IF EXISTS`` syntax is engine-side conditional
            # and runs even when the cursor's last error sticks. We
            # also log any drop failure visibly so an unexpected
            # tempdb issue can be diagnosed instead of silently
            # accumulating.
            try:
                cur.execute("DROP TABLE IF EXISTS tempdb..#sales_stage;")
            except Exception as drop_exc:
                logger.warning(
                    "sales_transactions UPSERT: temp-table cleanup "
                    "failed (will be reclaimed when pooled connection "
                    "closes): %s", drop_exc,
                )


# ---------------------------------------------------------------------------
# Cascade -- ask data_import to re-mirror the sales_transactions CSV
# ---------------------------------------------------------------------------

_CASCADE_RETRY_DELAY_SECONDS = 5.0


# Cache invalidation helper lives in ``services.cache_invalidation`` --
# see the module for the rationale. ArtifactService is deliberately NOT
# invalidated by the reconciliation cron: its cache is keyed on
# ``(path, st_mtime_ns, st_size)``, so the atomic-rename mtime flip
# data_import performs is itself the invalidation signal. Only the
# TTL-based VanLoadService needs an explicit poke.


def _post_cascade_once(
    s: Settings,
    *,
    dataset: str,
    lookback_days: int | None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Single HTTP attempt. Returns the data_import payload on success,
    a ``{"success": False, "error": ...}`` dict on any failure (network,
    timeout, non-2xx, JSON parse). The caller decides whether to retry."""
    import httpx
    base = (getattr(s, "data_import_url", None) or "").rstrip("/")
    body: dict[str, Any] = {"dataset": dataset, "mode": "incremental"}
    if lookback_days is not None:
        body["lookback_days"] = int(lookback_days)
    try:
        resp = httpx.post(
            f"{base}{s.data_import_path}",
            json=body,
            timeout=s.data_import_cascade_timeout_seconds,
            headers=_cron_propagation_headers(run_id),
        )
        resp.raise_for_status()
        payload = resp.json()
        return {
            "success": bool(payload.get("success")),
            "new_rows": payload.get("new_rows"),
            "total_rows": payload.get("total_rows"),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def _cascade_data_import_refresh(
    s: Settings,
    *,
    dataset: str,
    lookback_days: int | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """POST data_import to re-mirror the named dataset (sales_transactions
    after this refactor; demand_forecast for legacy back-compat).

    Order of operations:
      1. POST the cascade. May take 1-10s.
      2. On a clean transport failure, sleep ``_CASCADE_RETRY_DELAY_SECONDS``
         and retry exactly once. Bounded so a real outage surfaces fast.
      3. Invalidate the VanLoadService cache ONLY on success. The fresh
         CSV mtime then drives the cache-key change so the next read
         picks up the new content. Earlier code invalidated BEFORE the
         POST, which on cascade failure left the cache cold AND the CSV
         stale -- forcing every subsequent reader to repopulate the
         cache from stale CSV for the full TTL. Now: cascade failure
         leaves the cache warm with last-known-good data; recovery
         flips the CSV mtime, which busts the per-file cache key, and
         the next refresh's success invalidation picks up the new
         payload.
    """
    base = (getattr(s, "data_import_url", None) or "").rstrip("/")
    if not base:
        return {"skipped": True, "reason": "data_import_url not configured"}

    result = _post_cascade_once(s, dataset=dataset, lookback_days=lookback_days, run_id=run_id)
    if not result.get("success"):
        logger.warning(
            "reconciliation cascade refresh attempt 1 failed for %s: %s; "
            "retrying in %.1fs",
            dataset, result.get("error"), _CASCADE_RETRY_DELAY_SECONDS,
        )
        time.sleep(_CASCADE_RETRY_DELAY_SECONDS)
        result = _post_cascade_once(s, dataset=dataset, lookback_days=lookback_days, run_id=run_id)
        if not result.get("success"):
            logger.error(
                "reconciliation cascade refresh failed after retry for %s: %s",
                dataset, result.get("error"),
            )

    # Invalidate the van-load cache ONLY on a fully successful cascade.
    if result.get("success"):
        invalidate_van_load_cache(reason="reconciliation-refresh")

    return {"skipped": False, **result}


# ---------------------------------------------------------------------------
# Scheduler glue
# ---------------------------------------------------------------------------

def start_reconciliation_scheduler(
    *,
    settings: Settings | None = None,
    job: Callable[[], Any] | None = None,
    logger: Any | None = None,
) -> Any | None:
    """Daily 03:30 Dubai cron. Refreshes today + yesterday by default;
    callers can opt into a wider behind-window via direct API call."""
    s = settings or get_settings()
    log = logger or logging.getLogger(__name__)
    if not getattr(s, "reconciliation_refresh_enabled", True):
        log.info("reconciliation_refresh_disabled")
        return None
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        log.warning("APScheduler not installed -- reconciliation cron disabled")
        return None

    if job is None:
        def job() -> None:  # type: ignore[no-redef]
            # ``horizon_days_behind`` sourced from settings (default 1 =
            # today + yesterday) so the chain identity
            # ``leftover_to_next_day[d] == opening_stock[d+1]`` holds
            # across the cron boundary in a single pass.
            #
            # Wrapped in try/except so APScheduler's silent-swallow
            # behaviour can't hide an unhandled exception -- structured
            # log + traceback surface in ops dashboards.
            # ``force=False``: the data_import 03:00 cron now cascades
            # reconciliation directly, so by the time this 03:30 cron
            # fires the work is usually already done. The dedup guard
            # inside ``refresh_reconciliation`` checks the last-success
            # timestamp and short-circuits to a no-op when it fired
            # within the past 30 min. Backstop semantics preserved:
            # if the 03:00 cascade failed (network blip, downstream
            # outage), the last-success timestamp is stale and this
            # cron does the real work.
            try:
                res = refresh_reconciliation(
                    horizon_days_behind=int(
                        getattr(s, "reconciliation_refresh_horizon_days", 1)
                    ),
                    settings=s,
                    force=False,
                )
            except Exception:
                log.exception("reconciliation_refresh_unhandled_exception")
                return
            if res.get("skipped"):
                log.info(
                    "reconciliation_refresh_backstop_noop reason=%s elapsed=%ss "
                    "(primary 03:00 cascade succeeded; backstop has nothing to do)",
                    res.get("reason"), res.get("elapsed_seconds"),
                )
                return
            log.info(
                "reconciliation_refresh_done rows=%s window=%s duration=%s ok=%s",
                res.get("rows_updated", 0),
                res.get("window"),
                res.get("duration_seconds"),
                res.get("success"),
            )
            cascade = res.get("cascade") or {}
            if not cascade.get("skipped") and cascade.get("success") is False:
                log.error(
                    "reconciliation_cascade_failed: %s",
                    cascade.get("error"),
                )

    scheduler = BackgroundScheduler(
        timezone=s.reconciliation_refresh_timezone, daemon=True,
    )
    scheduler.add_job(
        job,
        trigger=CronTrigger(
            hour=s.reconciliation_refresh_hour,
            minute=s.reconciliation_refresh_minute,
            timezone=s.reconciliation_refresh_timezone,
        ),
        id="reconciliation_refresh",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    # Audit every fire to yf_scheduler_log so "did this cron run on
    # time?" is answerable from one DB row, independent of stdout.
    from common.scheduler_audit import attach_audit
    attach_audit(scheduler, "demand_forecasting", s.db.connection_string())
    scheduler.start()
    log.info(
        "reconciliation_scheduler_started cron=%02d:%02d tz=%s",
        s.reconciliation_refresh_hour,
        s.reconciliation_refresh_minute,
        s.reconciliation_refresh_timezone,
    )
    return scheduler


def stop_reconciliation_scheduler(scheduler: Any | None) -> None:
    if scheduler is None:
        return
    try:
        scheduler.shutdown(wait=False)
    except Exception as exc:
        logger.warning("reconciliation_scheduler shutdown error: %s", exc)
