"""
Daily reconciliation refresh.

Purpose
-------
Each forecast row in ``yf_demand_forecast`` carries four reconciled
columns (``recommended_load``, ``forecast_corrected``, ``bias_pct``,
``opening_stock``). Those values are populated by ``db_pusher`` at
training/inference time using whatever closing_stock + load_allocation
data was available then.

Operational reality moves daily: today's closing stock becomes
tomorrow's opening, depot allocations land each morning, and the
"recommended van load" tied to a future date stays accurate only if
the reconciliation is re-run against the freshest inputs. This service
does exactly that, on a daily schedule, for the rolling forecast window.

Contract
--------
- **Reads** the canonical DB mirror at ``imports/demand_forecast.csv``
  (kept fresh by ``data_import``). One read path, no DB-vs-mirror split.
- **Computes** via ``services.reconciliation.enrich.enrich_with_load`` --
  the same function ``db_pusher`` and the API lazy fallback use, so the
  three callers can never produce divergent values.
- **Writes** back to ``yf_demand_forecast`` via a #temp + MERGE that
  updates only the four reconciliation columns -- raw ``predicted`` and
  every other column stay untouched. No INSERT path: rows that don't
  already exist are deferred to the next training/inference push.
- **Idempotent** -- running with the same window twice is a no-op
  beyond the in-place column update.
- **Scoped** to ``data_split = 'Forecast'`` rows. Test-split predictions
  reflect a past inference run against past stock data; re-reconciling
  them with today's stock would corrupt the historical accuracy view.
- **Atomic** -- all rows in the window land in one transaction. A
  failure rolls back the whole window.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, Optional

import pandas as pd
import pyodbc

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.services.db_pool import FATAL_DB_ERRORS, get_pool
from demand_forecasting_pipeline.services.reconciliation.enrich import enrich_with_load

logger = logging.getLogger(__name__)


# Reconciliation columns -- mirror the engine's diagnostic outputs. Kept
# tuple-indexed so the SQL builder below can join on them. The two
# ``load_*_bound`` columns carry the ceil'd reconciled quantile band
# so the API can serve the leftover-aware ``Likely range`` directly
# from the DB instead of recomputing it on each request.
_RECON_COLS = (
    "recommended_load", "forecast_corrected", "bias_pct", "opening_stock",
    "load_lower_bound", "load_upper_bound",
)
# Natural key (matches db_pusher._MERGE_KEYS so an existing row in the
# target table is updated in place, never duplicated).
_KEY_COLS = ("trx_date", "route_code", "item_code", "data_split")


def refresh_reconciliation(
    *,
    horizon_days_ahead: int,
    horizon_days_behind: int = 0,
    data_splits: Iterable[str] = ("Forecast",),
    settings: Optional[Settings] = None,
    today: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Recompute the four reconciliation columns for the rolling window
    and UPDATE them in ``yf_demand_forecast``.

    Args:
        horizon_days_ahead:  inclusive upper bound, days from ``today``.
        horizon_days_behind: inclusive lower bound, days from ``today``
            (defaults to 0 = today). Useful for one-shot backfills.
        data_splits: which ``data_split`` values to cover. Defaults to
            ``('Forecast',)`` -- the daily steady-state cron only re-
            reconciles forward-looking forecasts. Pass
            ``('Forecast', 'Test')`` for a one-shot backfill that also
            covers historical test predictions.
        settings: optional override for tests; defaults to ``get_settings()``.
        today: optional override for tests; defaults to ``datetime.utcnow()``.

    Returns:
        Dict with ``success``, ``rows_updated``, ``window``,
        ``data_splits``, ``duration_seconds``, and on failure ``error``.
    """
    s = settings or get_settings()
    now = today or datetime.utcnow()
    start = (now - timedelta(days=int(horizon_days_behind))).date()
    end = (now + timedelta(days=int(horizon_days_ahead))).date()
    window = (str(start), str(end))
    splits_norm = tuple(str(x).strip().lower() for x in data_splits if str(x).strip())
    if not splits_norm:
        return {"success": False, "error": "data_splits is empty",
                "window": window}

    if not s.db.host or not s.demand_table:
        return {
            "success": False,
            "error": "DB not configured (set DF_DB_HOST + DF_DEMAND_TABLE)",
            "window": window,
        }

    t0 = pd.Timestamp.now()

    # 1. Read the DB mirror (kept fresh by data_import). Single source.
    src = s.shared_data_path(s.demand_forecast_file)
    if not src.exists():
        return {
            "success": False,
            "error": f"Mirror not found at {src}; run data_import refresh first",
            "window": window,
        }
    df = pd.read_csv(src, low_memory=False)
    if df.empty:
        return {"success": True, "rows_updated": 0, "window": window,
                "duration_seconds": 0.0, "message": "mirror empty"}

    # 2. Slice to the requested splits inside the window. The default
    #    ``('Forecast',)`` is what the daily cron uses; one-shot
    #    backfills that include ``Test`` opt in explicitly.
    df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce")
    df = df[df["DataSplit"].astype(str).str.strip().str.lower().isin(splits_norm)]
    df = df[(df["TrxDate"].dt.date >= start) & (df["TrxDate"].dt.date <= end)]
    if df.empty:
        return {
            "success": True, "rows_updated": 0, "window": window,
            "data_splits": list(data_splits),
            "duration_seconds": round((pd.Timestamp.now() - t0).total_seconds(), 2),
            "message": "no rows in window for the requested splits",
        }

    # 3. Run the canonical engine -- same function db_pusher and the API use.
    enriched = enrich_with_load(
        df,
        predicted_col="Predicted",
        output_col="recommended_load",
        with_diagnostics=True,
        settings=s,
    )

    missing = [c for c in _RECON_COLS if c not in enriched.columns]
    if missing:
        return {
            "success": False,
            "error": f"enrich_with_load did not produce columns {missing}",
            "window": window,
        }

    # 4. Project to the DB shape -- one row per natural key, four floats.
    #    Carry the original split through (capitalised to match how
    #    db_pusher writes it: ``Forecast`` / ``Test``) so the MERGE join
    #    targets the exact source row.
    update_df = pd.DataFrame({
        "trx_date":    enriched["TrxDate"].dt.strftime("%Y-%m-%d"),
        "route_code":  enriched["RouteCode"].astype(str),
        "item_code":   enriched["ItemCode"].astype(str),
        "data_split":  enriched["DataSplit"].astype(str).str.strip().str.capitalize(),
    })
    for c in _RECON_COLS:
        update_df[c] = pd.to_numeric(enriched[c], errors="coerce").fillna(0.0).astype(float)

    rows_updated = _merge_update(s, update_df)

    # Cascade: re-pull the same window we just rewrote (+ a small safety
    # buffer) so the CSV mirror reflects every row we touched. Without
    # ``lookback_days``, pure-append incremental would miss every UPDATE
    # whose date already exists in the CSV -- exactly the rolling window
    # we re-reconcile each day. The buffer covers timezone drift between
    # this process and the data_import worker.
    cascade_lookback = max(int(horizon_days_behind) + int(horizon_days_ahead) + 2, 7)
    cascade = _cascade_data_import_refresh(s, lookback_days=cascade_lookback)

    return {
        "success": True,
        "rows_updated": rows_updated,
        "window": window,
        "data_splits": list(data_splits),
        "duration_seconds": round((pd.Timestamp.now() - t0).total_seconds(), 2),
        "cascade": cascade,
    }


def _cascade_data_import_refresh(
    s: Settings,
    *,
    lookback_days: Optional[int] = None,
) -> Dict[str, Any]:
    """POST data_import to re-mirror the demand_forecast table.

    ``lookback_days`` opts into the importer's refresh-window mode: it
    re-pulls the last N days regardless of CSV state and dedup-merges,
    so row UPDATEs from this refresh propagate to the mirror. Without
    it, pure-append incremental misses any update on a date that
    already exists in the CSV. Skips silently if ``DF_DATA_IMPORT_URL``
    is unset (production deployments that orchestrate data_import on a
    separate schedule).
    """
    base = (s.data_import_url or "").rstrip("/") if hasattr(s, "data_import_url") else ""
    if not base:
        return {"skipped": True, "reason": "data_import_url not configured"}
    try:
        import httpx
        body: Dict[str, Any] = {"dataset": s.data_import_dataset, "mode": "incremental"}
        if lookback_days is not None:
            body["lookback_days"] = int(lookback_days)
        resp = httpx.post(
            f"{base}{s.data_import_path}",
            json=body,
            timeout=s.data_import_cascade_timeout_seconds,
        )
        resp.raise_for_status()
        payload = resp.json()
        ok = bool(payload.get("success"))
        if ok:
            # The mirror CSV just changed under VanLoadService's cache
            # (per-(route, date) keyed, ~5-min TTL). Drop it so the next
            # /reconciliation/recommend or /workflow/plan request sees
            # the freshly mirrored composition instead of the stale one.
            try:
                from demand_forecasting_pipeline.api.dependencies import get_van_load_service
                get_van_load_service().invalidate()
            except Exception as inv_exc:
                logger.warning(
                    "van_load_cache_invalidate_failed after cascade: %s", inv_exc,
                )
        return {
            "skipped": False,
            "success": ok,
            "new_rows": payload.get("new_rows"),
            "total_rows": payload.get("total_rows"),
        }
    except Exception as exc:
        # Promoted from warning to error -- cascade failure leaves the
        # mirror CSV one cycle behind the DB; ops needs to see this in
        # alerting so closing_stock staleness is caught before the next
        # generation cron consumes it.
        logger.error(
            "ALERT reconciliation cascade refresh failed: %s -- "
            "imports/demand_forecast.csv is now one cycle behind yf_demand_forecast; "
            "next data_import run at 03:00 will re-converge",
            exc,
        )
        return {"skipped": False, "success": False, "error": str(exc)}


def _merge_update(s: Settings, update_df: pd.DataFrame) -> int:
    """Stage -> MERGE the update set into the target table.

    Mirrors ``db_pusher._upsert``'s pattern: bulk INSERT to a #temp
    staging table outside the target lock window, then MERGE inside a
    single transaction so the target's lock window is bounded to the
    matching join. UPDATE-only -- WHEN NOT MATCHED is intentionally
    omitted so we never insert reconciliation-only rows that lack a
    matching prediction.
    """
    if update_df.empty:
        return 0

    cols = list(update_df.columns)
    placeholders = ", ".join("?" for _ in cols)
    col_list_sql = ", ".join(f"[{c}]" for c in cols)

    pool = get_pool(
        s.db.connection_string(),
        max_connections=max(s.db.retry_attempts + 1, 4),
        connect_timeout=s.db.connection_timeout,
        query_timeout=s.db.query_timeout,
        autocommit=False,
    )
    with pool.acquire() as conn:
        cur = conn.cursor()
        try:
            cur.fast_executemany = True
            # 1. Staging table
            cur.execute(f"""
                CREATE TABLE #recon_stage (
                    [trx_date]            DATE NOT NULL,
                    [route_code]          NVARCHAR(50) NOT NULL,
                    [item_code]           NVARCHAR(50) NOT NULL,
                    [data_split]          NVARCHAR(20) NOT NULL,
                    [recommended_load]    FLOAT NOT NULL DEFAULT 0,
                    [forecast_corrected]  FLOAT NOT NULL DEFAULT 0,
                    [bias_pct]            FLOAT NOT NULL DEFAULT 0,
                    [opening_stock]       FLOAT NOT NULL DEFAULT 0,
                    [load_lower_bound]    FLOAT NOT NULL DEFAULT 0,
                    [load_upper_bound]    FLOAT NOT NULL DEFAULT 0
                );
            """)
            # 2. Bulk insert
            records = [tuple(r) for r in update_df.itertuples(index=False, name=None)]
            cur.executemany(
                f"INSERT INTO #recon_stage ({col_list_sql}) VALUES ({placeholders})",
                records,
            )
            # 3. MERGE -- update only, never insert. ON-clause uses the
            #    natural key. SET-clause updates the four reconciled cols.
            set_clause = ", ".join(f"T.[{c}] = S.[{c}]" for c in _RECON_COLS)
            on_clause = " AND ".join(f"T.[{k}] = S.[{k}]" for k in _KEY_COLS)
            cur.execute(f"""
                MERGE {s.demand_table} AS T
                USING #recon_stage AS S
                  ON {on_clause}
                WHEN MATCHED THEN
                    UPDATE SET {set_clause};
            """)
            rows = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            conn.commit()
            return int(rows)
        except FATAL_DB_ERRORS as exc:
            conn.rollback()
            logger.error("reconciliation refresh: fatal DB error: %s", exc)
            raise
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            try:
                cur.execute("IF OBJECT_ID('tempdb..#recon_stage') IS NOT NULL DROP TABLE #recon_stage;")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Scheduler glue
# ---------------------------------------------------------------------------


def start_reconciliation_scheduler(
    *,
    settings: Optional[Settings] = None,
    job: Optional[Callable[[], Any]] = None,
    logger: Optional[Any] = None,
) -> Optional[Any]:
    """Start a daily APScheduler cron at the configured wall-clock time
    (``reconciliation_refresh_hour``:``reconciliation_refresh_minute`` in
    the configured timezone). Returns the scheduler so the lifespan can
    shut it down on app exit. Returns None if APScheduler is unavailable
    or the schedule is disabled by config.
    """
    s = settings or get_settings()
    log = logger or logging.getLogger(__name__)
    if not s.reconciliation_refresh_enabled:
        log.info("reconciliation_refresh_disabled")
        return None
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        log.warning(
            "APScheduler not installed -- reconciliation refresh cron disabled"
        )
        return None

    if job is None:
        def job() -> None:  # type: ignore[no-redef]
            res = refresh_reconciliation(
                horizon_days_ahead=s.reconciliation_refresh_horizon_days,
                horizon_days_behind=0,
                settings=s,
            )
            log.info(
                "reconciliation_refresh_done",
                rows_updated=res.get("rows_updated", 0),
                window=res.get("window"),
                duration_seconds=res.get("duration_seconds"),
                success=res.get("success"),
                error=res.get("error"),
            )
            # Surface a cascade failure as a separate ERROR-level event
            # so monitoring picks it up. The refresh itself succeeded
            # (DB cols were UPDATEd); only the mirror-CSV cascade is
            # behind. The 04:30 generation's pre-refresh covers the gap
            # but ops should still know.
            cascade = res.get("cascade") or {}
            if not cascade.get("skipped") and cascade.get("success") is False:
                log.error(
                    "reconciliation_cascade_failed",
                    error=cascade.get("error"),
                    impact="imports/demand_forecast.csv lags DB by one cycle",
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
        "reconciliation_scheduler_started",
        cron=f"{s.reconciliation_refresh_hour:02d}:{s.reconciliation_refresh_minute:02d}",
        timezone=s.reconciliation_refresh_timezone,
        horizon_days=s.reconciliation_refresh_horizon_days,
    )
    return scheduler


def stop_reconciliation_scheduler(scheduler: Optional[Any]) -> None:
    if scheduler is None:
        return
    try:
        scheduler.shutdown(wait=False)
    except Exception as exc:
        logger.warning("reconciliation_scheduler shutdown error: %s", exc)
