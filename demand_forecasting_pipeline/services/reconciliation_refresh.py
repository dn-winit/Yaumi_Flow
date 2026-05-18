"""
Daily reconciliation refresh -- writes the carry chain + diagnostics +
actual_sold for past + today into ``yf_sales_transactions``.

Architecture
------------
``yf_demand_forecast`` is purely the model output (predicted, p_demand,
bounds, demand_class, etc.). The reconciliation columns (carry chain,
engine math, envelope diagnostics) plus the actual_sold reality side
live in ``yf_sales_transactions``. This module is the writer.

Contract
--------
- **Reads** the demand_forecast CSV mirror (kept fresh by data_import)
  for raw model input, AND ``VW_GET_SALES_DETAILS`` for actual_sold.
- **Computes** via ``services.reconciliation.enrich.enrich_with_load``
  -- the same engine the recommendation pipeline and the API lazy
  fallback use.
- **Writes** to ``yf_sales_transactions`` via #temp + MERGE with the
  natural key ``(trx_date, route_code, item_code)``. UPSERT semantics
  so the daily cron covers both new today-rows and refreshes of
  yesterday/etc.
- **Idempotent** -- same inputs, same outputs, byte-identical.
- **Past + today only**. Future dates are excluded by design (no actual
  transactions yet -- can't reconcile what hasn't happened).
- **Atomic per window** -- all rows land in one transaction.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, Optional

import pandas as pd
import pyodbc

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from common.db_pool import FATAL_DB_ERRORS, get_pool
from demand_forecasting_pipeline.services.reconciliation.enrich import (
    enrich_with_load,
    forward_fill_closing,
)

logger = logging.getLogger(__name__)


# In-process serialisation for ``refresh_reconciliation``. Two concurrent
# calls on overlapping (start, end) windows would race the MERGE -- SQL
# Server's SERIALIZABLE level prevents corrupt rows, but the second writer
# can still error out on a key-range lock collision and abandon its own
# cascade. The lock keeps the second caller queued behind the first so
# the cron, the API trigger, and any manual replay produce identical
# state regardless of arrival order.
#
# Locking discipline (CRITICAL): the lock is held ONLY across the DB
# write. It is released BEFORE the cascade HTTP POST so a slow cascade
# can never block a second writer indefinitely (the cascade is itself
# retried inside ``_cascade_data_import_refresh`` -- holding the global
# lock across it would compound the wait).
_REFRESH_LOCK = threading.Lock()

# Last successful refresh timestamp (UTC). Read under ``_REFRESH_LOCK``.
# Used by the cron-context call below to short-circuit when the
# data_import cascade already ran reconciliation within the last
# ``CRON_SKIP_IF_RECENT_SECONDS`` window. Manual ``/refresh`` calls
# bypass this guard via ``force=True`` so operators can always trigger
# a fresh run on demand.
_LAST_REFRESH_AT: Optional[datetime] = None
CRON_SKIP_IF_RECENT_SECONDS = 1800  # 30 min


# Engine output column -> yf_sales_transactions column name.
# Single source of truth for the renaming; the SQL builder below uses
# the values, the projection step uses the keys.
_RECON_COL_MAP: Dict[str, str] = {
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

# Natural key. No data_split -- yf_sales_transactions is split-agnostic
# (transactions are a real-world fact; the split is a model concept).
_KEY_COLS = ("trx_date", "route_code", "item_code")

# Boolean-typed target columns -- staged as BIT in temp table.
_BOOL_COLS = {
    "pattern_floor_applied",
    "pattern_ceiling_applied",
    "forecast_below_recent",
    "forecast_dormant",
}

_SALES_TARGET_TABLE = "[YaumiAIML].[dbo].[yf_sales_transactions]"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def refresh_reconciliation(
    *,
    horizon_days_behind: int = 0,
    settings: Optional[Settings] = None,
    today: Optional[datetime] = None,
    force: bool = True,
) -> Dict[str, Any]:
    """Recompute the carry chain + diagnostics for ``[today-behind, today]``
    and UPSERT into yf_sales_transactions.

    Args:
        horizon_days_behind: How many days back from today to refresh.
            ``0`` = today only; daily cron default is 1 (today + yesterday). Wider
            values support backfill-style re-runs across past dates.
        settings: optional override for tests.
        today: optional override for tests.
        force: When ``False``, the call short-circuits if another caller
            already ran a successful refresh within
            ``CRON_SKIP_IF_RECENT_SECONDS``. This is the cron-context
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
    now = today or datetime.now(timezone.utc)
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
            elapsed = (datetime.now(timezone.utc) - last).total_seconds()
            if elapsed < CRON_SKIP_IF_RECENT_SECONDS:
                logger.info(
                    "reconciliation_refresh_skipped_recent: last successful "
                    "refresh was %.0fs ago (<%ds threshold); 03:00 cascade "
                    "already covered this window",
                    elapsed, CRON_SKIP_IF_RECENT_SECONDS,
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
        engine_actuals: dict[tuple[str, str, "pd.Timestamp"], float] = {}
        engine_latest_actual: Optional["pd.Timestamp"] = None
        if not engine_actuals_df.empty:
            for r in engine_actuals_df.itertuples(index=False):
                key = (str(r.route_code), str(r.item_code),
                       pd.Timestamp(r.trx_date).normalize())
                engine_actuals[key] = float(r.actual_sold or 0.0)
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
        rows_updated = _upsert_sales_transactions(s, write_df)
        # Stamp last-successful-refresh BEFORE releasing the lock so a
        # concurrent backstop tick reading the timestamp sees the most
        # recent value under the same critical section that wrote it.
        # ``global`` already declared at the top of the function.
        _LAST_REFRESH_AT = datetime.now(timezone.utc)
    finally:
        # Release BEFORE the cascade so a slow / retried HTTP round-trip
        # can never block a subsequent caller. The DB MERGE above is the
        # only step that needs serialisation.
        _REFRESH_LOCK.release()

    # 9. Cascade the new CSV mirror (sales_transactions dataset). The
    #    cascade helper invalidates the VanLoadService cache BEFORE the
    #    POST so concurrent readers can't observe a pre-refresh slot
    #    during the round-trip; on cascade failure the cache stays cold
    #    and the next read re-populates from the last-known-good CSV.
    cascade_lookback = max(int(horizon_days_behind) + 2, 7)
    cascade = _cascade_data_import_refresh(
        s,
        dataset="sales_transactions",
        lookback_days=cascade_lookback,
    )

    # 10. Cascade -> recommended_order regenerates for each refreshed date.
    recommended_order_cascade = _cascade_recommended_order_generate(s, window=window)

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
    payload: Dict[str, Any] = {
        "success": cascade_ok and ro_ok,
        "rows_updated": int(rows_updated),
        "window": window,
        "duration_seconds": round((pd.Timestamp.now() - t0).total_seconds(), 2),
        "cascade": cascade,
        "recommended_order_cascade": recommended_order_cascade,
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
    return payload


def _cascade_recommended_order_generate(
    s: Settings, *, window: tuple[str, str],
) -> Dict[str, Any]:
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

    def _post_once(d: str) -> Dict[str, Any]:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(
                f"{url_base}/api/v1/recommended-order/generate",
                json={"date": d, "force": True},
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

    results: list[Dict[str, Any]] = []
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


# ---------------------------------------------------------------------------
# Actual sold lookup (read-only YaumiLive)
# ---------------------------------------------------------------------------

def _fetch_actual_sold(
    s: Settings,
    start: Any,
    end: Any,
) -> pd.DataFrame:
    """Pull SUM(QuantityInPCs) per (route, item, date) from
    VW_GET_SALES_DETAILS for the [start, end] window. YaumiLive is
    READ-ONLY -- this is a query, never a write.

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
    route_clause = ""
    params: list = [str(start), str(end)]
    if routes:
        route_clause = f"AND RouteCode IN ({','.join(['?'] * len(routes))})"
        params.extend(str(r) for r in routes)

    sql = f"""
    SELECT
        CAST(TrxDate AS DATE) AS trx_date,
        RouteCode             AS route_code,
        ItemCode              AS item_code,
        SUM(QuantityInPCs)    AS actual_sold
    FROM [YaumiLive].[dbo].[VW_GET_SALES_DETAILS] WITH (NOLOCK)
    WHERE ItemType = 'OrderItem'
      AND TrxType  = 'SalesInvoice'
      AND QuantityInPCs > 0
      AND CAST(TrxDate AS DATE) BETWEEN ? AND ?
      {route_clause}
    GROUP BY CAST(TrxDate AS DATE), RouteCode, ItemCode;
    """
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

def _upsert_sales_transactions(s: Settings, write_df: pd.DataFrame) -> int:
    """Stage -> MERGE write_df into yf_sales_transactions.

    UPSERT semantics: WHEN MATCHED THEN UPDATE, WHEN NOT MATCHED THEN
    INSERT. The cron's first call of the day creates today's rows; the
    second call updates them with fresh actuals.
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

    # SQL Server doesn't accept Python None alongside floats via pyodbc
    # fast_executemany consistently; we ship NaN for missing numerics
    # and None only for the BIT cols. Build the records tuple in the
    # exact order of staging columns.
    all_cols = list(_KEY_COLS) + target_cols
    records = []
    for _, r in write_df.iterrows():
        row = []
        for c in all_cols:
            v = r.get(c)
            if c in _BOOL_COLS:
                row.append(int(v) if pd.notna(v) else 0)
            elif c in _KEY_COLS:
                row.append(str(v))
            else:
                row.append(float(v) if pd.notna(v) else None)
        records.append(tuple(row))

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
    set_clause = ", ".join(f"T.[{c}] = S.[{c}]" for c in target_cols + ["updated_at"])
    on_clause = " AND ".join(f"T.[{k}] = S.[{k}]" for k in _KEY_COLS)

    with pool.acquire() as conn:
        cur = conn.cursor()
        try:
            cur.fast_executemany = True
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
                MERGE {_SALES_TARGET_TABLE} AS T
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
            except Exception:
                pass
            raise
        finally:
            try:
                cur.execute("IF OBJECT_ID('tempdb..#sales_stage') IS NOT NULL DROP TABLE #sales_stage;")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Cascade -- ask data_import to re-mirror the sales_transactions CSV
# ---------------------------------------------------------------------------

_CASCADE_RETRY_DELAY_SECONDS = 5.0


def _invalidate_van_load_cache() -> None:
    """Drop the in-process VanLoadService cache so the next read repopulates
    from the freshly-mirrored CSV.

    ArtifactService is NOT invalidated here: its cache is keyed on
    ``(path, st_mtime_ns, st_size)`` so the CSV mtime change that
    data_import performs on the atomic rename is itself the invalidation
    signal -- the next ``_read_df`` observes a new snapshot tuple and
    re-parses. Adding an explicit ``invalidate_cache()`` call would be a
    redundant memory-only hint that doesn't change correctness. The
    VanLoadService cache, by contrast, is TTL-based and cannot see the
    mtime flip, so it does need an explicit poke.
    """
    try:
        from demand_forecasting_pipeline.api.dependencies import get_van_load_service
        get_van_load_service().invalidate()
    except Exception as inv_exc:
        logger.warning(
            "van_load_cache_invalidate_failed before cascade: %s", inv_exc,
        )


def _post_cascade_once(
    s: Settings,
    *,
    dataset: str,
    lookback_days: Optional[int],
) -> Dict[str, Any]:
    """Single HTTP attempt. Returns the data_import payload on success,
    a ``{"success": False, "error": ...}`` dict on any failure (network,
    timeout, non-2xx, JSON parse). The caller decides whether to retry."""
    import httpx
    base = (getattr(s, "data_import_url", None) or "").rstrip("/")
    body: Dict[str, Any] = {"dataset": dataset, "mode": "incremental"}
    if lookback_days is not None:
        body["lookback_days"] = int(lookback_days)
    try:
        resp = httpx.post(
            f"{base}{s.data_import_path}",
            json=body,
            timeout=s.data_import_cascade_timeout_seconds,
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
    lookback_days: Optional[int] = None,
) -> Dict[str, Any]:
    """POST data_import to re-mirror the named dataset (sales_transactions
    after this refactor; demand_forecast for legacy back-compat).

    Order of operations (race-free):
      1. Invalidate the VanLoadService cache. From here on, any concurrent
         read repopulates from disk -- worst case it re-reads the old CSV
         once more, but the next read after data_import flips the mtime
         picks up the fresh content automatically.
      2. POST the cascade. May take 1-10s.
      3. On a clean transport failure, sleep ``_CASCADE_RETRY_DELAY_SECONDS``
         and retry exactly once. Bounded so a real outage surfaces fast.
      4. Return success / failure to the caller.

    On cascade failure the cache stays cold -- next read repopulates
    from whatever the CSV currently is (last known good if data_import
    hadn't started overwriting yet, or partial mid-write content; either
    way it converges once data_import recovers).
    """
    base = (getattr(s, "data_import_url", None) or "").rstrip("/")
    if not base:
        return {"skipped": True, "reason": "data_import_url not configured"}

    _invalidate_van_load_cache()

    result = _post_cascade_once(s, dataset=dataset, lookback_days=lookback_days)
    if not result.get("success"):
        logger.warning(
            "reconciliation cascade refresh attempt 1 failed for %s: %s; "
            "retrying in %.1fs",
            dataset, result.get("error"), _CASCADE_RETRY_DELAY_SECONDS,
        )
        time.sleep(_CASCADE_RETRY_DELAY_SECONDS)
        _invalidate_van_load_cache()
        result = _post_cascade_once(s, dataset=dataset, lookback_days=lookback_days)
        if not result.get("success"):
            logger.error(
                "reconciliation cascade refresh failed after retry for %s: %s",
                dataset, result.get("error"),
            )

    return {"skipped": False, **result}


# ---------------------------------------------------------------------------
# Scheduler glue
# ---------------------------------------------------------------------------

def start_reconciliation_scheduler(
    *,
    settings: Optional[Settings] = None,
    job: Optional[Callable[[], Any]] = None,
    logger: Optional[Any] = None,
) -> Optional[Any]:
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
    scheduler.start()
    log.info(
        "reconciliation_scheduler_started cron=%02d:%02d tz=%s",
        s.reconciliation_refresh_hour,
        s.reconciliation_refresh_minute,
        s.reconciliation_refresh_timezone,
    )
    return scheduler


def stop_reconciliation_scheduler(scheduler: Optional[Any]) -> None:
    if scheduler is None:
        return
    try:
        scheduler.shutdown(wait=False)
    except Exception as exc:
        logger.warning("reconciliation_scheduler shutdown error: %s", exc)
