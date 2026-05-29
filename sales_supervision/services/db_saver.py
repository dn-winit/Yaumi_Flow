"""
Per-visit upsert of supervision data into YaumiAIML (routes / customers / items).
Column order matches scripts/create_tables.sql; YaumiLive is read-only here.
Idempotent on (session_id, customer_code): retries and double-clicks are safe.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import pyodbc

# Shared bounded AIML pool across all YaumiAIML writers.
from common.db_pool import get_pool, with_db_retry
from common.numeric import safe_float as _to_float
from common.numeric import safe_int as _to_int
from sales_supervision.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# Column orders matching create_tables.sql.
# Excludes IDENTITY (id) and PERSISTED computed columns.
_ROUTE_COLS = [
    "session_id", "route_code", "supervision_date",
    "customers_planned", "customers_visited",
    "planned_qty_recommended", "visited_qty_recommended", "visited_qty_actual",
    "route_performance_score",
    "session_status", "session_started_at", "session_completed_at",
]

_CUSTOMER_COLS = [
    "session_id", "customer_code", "customer_name", "visit_sequence",
    "skus_recommended", "skus_sold", "sku_coverage_rate",
    "qty_recommended", "qty_actual",
    "customer_accuracy_avg", "customer_performance_score",
    "record_saved_at",
]

_ITEM_COLS = [
    "session_id", "customer_code", "item_code", "item_name",
    "original_recommended_qty", "adjusted_recommended_qty",
    "actual_qty", "was_manually_edited", "was_item_sold",
    "recommendation_tier", "priority_score", "van_inventory_qty",
    "days_since_last_purchase", "purchase_cycle_days", "purchase_frequency_pct",
    "record_saved_at",
]


def _insert_sql(table_placeholder: str, cols: list[str]) -> str:
    """Build parametric INSERT template; ``{table}`` slot filled at runtime."""
    col_list = ", ".join(f"[{c}]" for c in cols)
    placeholders = ", ".join("?" for _ in cols)
    return f"INSERT INTO {table_placeholder} ({col_list}) VALUES ({placeholders})"


def _merge_sql(
    table_placeholder: str,
    all_cols: list[str],
    update_cols: list[str],
    key_cols: list[str],
    *,
    lock_hints: str = "HOLDLOCK, UPDLOCK",
) -> str:
    """Build atomic single-row MERGE upsert; HOLDLOCK + UPDLOCK prevents concurrent-insert race.

    ``update_cols`` is the matched-row mutation subset; exclude LLM payload columns so
    per-visit upserts never clobber previously-saved briefings/analyses.
    """
    src_cols_csv = ", ".join(f"[{c}]" for c in all_cols)
    placeholders = ", ".join("?" for _ in all_cols)
    on_clause   = " AND ".join(f"tgt.[{k}] = src.[{k}]" for k in key_cols)
    update_set  = ", ".join(f"tgt.[{c}] = src.[{c}]" for c in update_cols)
    insert_cols = ", ".join(f"[{c}]" for c in all_cols)
    insert_vals = ", ".join(f"src.[{c}]" for c in all_cols)
    hints       = f" WITH ({lock_hints})" if lock_hints else ""
    return (
        f"MERGE {table_placeholder}{hints} AS tgt "
        f"USING (VALUES ({placeholders})) AS src ({src_cols_csv}) "
        f"ON ({on_clause}) "
        f"WHEN MATCHED THEN UPDATE SET {update_set} "
        f"WHEN NOT MATCHED THEN INSERT ({insert_cols}) VALUES ({insert_vals});"
    )


# Mutable per-visit columns. Excludes natural key and session_started_at /
# session_completed_at -- those are INSERT-only so cron upserts can't clobber them.
_ROUTE_UPDATE_COLS = [
    "route_code", "supervision_date",
    "customers_planned", "customers_visited",
    "planned_qty_recommended", "visited_qty_recommended", "visited_qty_actual",
    "route_performance_score",
    "session_status",
]

# Mutable per-visit columns; excludes composite key and LLM payload columns (owned by dedicated save paths).
_CUSTOMER_UPDATE_COLS = [
    "customer_name", "visit_sequence",
    "skus_recommended", "skus_sold", "sku_coverage_rate",
    "qty_recommended", "qty_actual",
    "customer_accuracy_avg", "customer_performance_score",
    "record_saved_at",
]

# Pre-built SQL templates with ``{table}`` slot. Routes/customers use atomic MERGE;
# items use DELETE + INSERT scoped to (session_id, customer_code) (race is at customer level).
_ROUTE_MERGE_TPL    = _merge_sql("{table}", _ROUTE_COLS,    _ROUTE_UPDATE_COLS,    ["session_id"])
_CUSTOMER_MERGE_TPL = _merge_sql("{table}", _CUSTOMER_COLS, _CUSTOMER_UPDATE_COLS, ["session_id", "customer_code"])
_ITEM_INSERT_TPL    = _insert_sql("{table}", _ITEM_COLS)
_ITEMS_DELETE_TPL   = "DELETE FROM {table} WHERE [session_id] = ? AND [customer_code] = ?"

# Per-visit session_status -- the row stays 'active' as long as visits
# are landing. Closing a session is an explicit, separate action that
# would set this column to 'closed' along with session_completed_at.
_STATUS_ACTIVE = "active"

# ---------------------------------------------------------------------------
# Defensive scalar coercion helpers.
#
# The session JSON snapshot is built by trusted server code, but some fields
# round-trip through the browser (e.g. supervisor edits a quantity in the UI)
# and arrive as strings or floats. These helpers guarantee the shape the
# schema requires, so a single bad row never aborts the whole executemany.
# ---------------------------------------------------------------------------

# Every percent / score column in the schema is DECIMAL(8,2), so a
# single clamp helper covers them all. ``DECIMAL(8,2)`` accepts up to
# 999_999.99; we clamp at that boundary so a runaway upstream value
# (a regressed score, a buggy upstream percent) can never trip a
# numeric-overflow during executemany.
#
# Formula-derived rates (customer_completion_rate, qty_fulfillment_rate,
# recommendation_adjustment) live as PERSISTED computed columns now,
# so the writer no longer has to compute or clamp them -- the schema
# owns the math.
_DECIMAL_8_2_MAX = 999_999.99


def _clamp_score(value: Any) -> float:
    """Bound a value to ``DECIMAL(8,2)`` range so SQL Server never
    raises a numeric-overflow during the bulk insert."""
    f = _to_float(value)
    if f > _DECIMAL_8_2_MAX:
        return _DECIMAL_8_2_MAX
    if f < -_DECIMAL_8_2_MAX:
        return -_DECIMAL_8_2_MAX
    return f


# Customer-name length matches schema NVARCHAR(255).
_LEN_CUSTOMER_NAME = 255


def _str_clip(value: Any, max_len: int) -> str:
    """Coerce to string and clip at the schema's NVARCHAR length so we
    never trip a "String or binary data would be truncated" error.

    Emits a WARNING when truncation actually happens so a column whose
    real-world value has outgrown the schema becomes visible in ops
    logs instead of silently losing the tail. The first 32 chars of
    the original are included so an operator can grep for the source
    record without dumping the whole payload.
    """
    if value is None:
        return ""
    s = str(value)
    if len(s) <= max_len:
        return s
    logger.warning(
        "supervision saver truncated %d-char string -> %d (head=%r)",
        len(s), max_len, s[:32],
    )
    return s[:max_len]


def _empty_redistribution_dict() -> dict[str, Any]:
    """Pydantic-driven empty RedistributionView dump. Single source of
    truth so a future wire-schema field is reflected here automatically."""
    from sales_supervision.api.schemas import RedistributionView
    return RedistributionView().model_dump()


# NVARCHAR limits per scripts/create_tables.sql. The 'session_id' column is
# defined NVARCHAR(100) on all three tables (foreign key alignment).
_LEN_SESSION_ID = 100
_LEN_ROUTE_CODE = 50
_LEN_CUSTOMER_CODE = 50
_LEN_ITEM_CODE = 50
_LEN_ITEM_NAME = 255
_LEN_TIER = 50
_LEN_STATUS = 20


class _SkipReplay(Exception):
    """Sentinel: caller asked to defer redistribution replay."""


class DbSaver:
    """Pushes supervision session data to 3 DB tables in YaumiAIML."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()
        self._db = self._s.db
        # Cache for load_session_visits; invalidated by reconcile_route + /visit, TTL backstop.
        # Stores (monotonic_ts, payload); payload=None caches "no rows yet" answers too.
        self._saved_visits_cache: dict[
            tuple[str, str, bool],
            tuple[float, dict[str, Any] | None],
        ] = {}
        self._saved_visits_cache_lock = threading.Lock()

    @property
    def available(self) -> bool:
        return (
            self._db.configured
            and bool(self._s.route_summary_table)
            and bool(self._s.customer_summary_table)
            and bool(self._s.item_details_table)
        )

    def invalidate_saved_visits(self, route_code: str, date: str) -> None:
        """Drop cached load_session_visits entries for (route, date); called after a fresh write."""
        rc, d = str(route_code), str(date)
        with self._saved_visits_cache_lock:
            # Drop both include_redistributions=False/True variants in one pass.
            stale = [k for k in self._saved_visits_cache if k[0] == rc and k[1] == d]
            for k in stale:
                self._saved_visits_cache.pop(k, None)

    def _pool(self):
        """Resolve the shared AIML pool; cap floors at data_phase_workers."""
        cap = max(
            int(self._s.db_pool_max_connections),
            int(self._s.auto_visit_data_phase_workers),
        )
        return get_pool(
            self._db.connection_string(),
            max_connections=cap,
            connect_timeout=int(self._db.connection_timeout),
            query_timeout=int(self._db.query_timeout),
            autocommit=False,
        )

    @contextmanager
    def _open_conn(self) -> Iterator[pyodbc.Connection]:
        """Yield a pooled connection; pool's acquire() handles close + semaphore release."""
        with self._pool().acquire() as conn:
            yield conn

    # ------------------------------------------------------------------
    # Per-visit upsert
    # ------------------------------------------------------------------

    def upsert_visit(self, snapshot: dict[str, Any], customer_code: str) -> dict[str, Any]:
        """Single-transaction upsert of route header + one customer + their items.

        Idempotent on (session_id, customer_code). Called per process_visit, typically
        as a BackgroundTask so the response isn't blocked on warehouse latency.
        """
        if not self.available:
            return {"success": False, "error": "DB not configured (set SS_DB_HOST + table names)"}

        sid = snapshot.get("sessionId", "")
        customer = snapshot.get("customers", {}).get(customer_code)
        if not sid or customer is None:
            return {
                "success": False,
                "error": f"snapshot missing sessionId or customer {customer_code!r}",
            }

        now = datetime.now()
        # supervision_date is NOT NULL DATE; fall back to today if absent.
        sup_date = snapshot.get("date") or now.strftime("%Y-%m-%d")

        try:
            with self._open_conn() as conn:
                cursor = conn.cursor()
                cursor.fast_executemany = True
                try:
                    self._upsert_route_header(cursor, sid, snapshot, sup_date, now)
                    self._upsert_customer(cursor, sid, customer_code, customer, now)
                    item_count = self._replace_customer_items(
                        cursor, sid, customer_code, customer, now,
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

            logger.info(
                "Visit upsert ok: sid=%s cust=%s items=%d",
                sid, customer_code, item_count,
            )
            return {
                "success": True,
                "session_id": sid,
                "customer_code": customer_code,
                "items": item_count,
            }
        except Exception as exc:
            logger.error(
                "Visit upsert failed (sid=%s cust=%s): %s",
                sid, customer_code, exc,
            )
            return {"success": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def refresh_route_header(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Stamp the route header from snapshot without touching customer/item rows.

        Called every reconcile_route tick so header columns always reflect the latest
        in-memory state, even on no-op ticks.
        """
        if not self.available:
            return {"success": False, "error": "DB not configured"}
        sid = snapshot.get("sessionId", "")
        if not sid:
            return {"success": False, "error": "snapshot missing sessionId"}
        now = datetime.now()
        sup_date = snapshot.get("date") or now.strftime("%Y-%m-%d")
        try:
            with self._open_conn() as conn:
                cursor = conn.cursor()
                try:
                    self._upsert_route_header(cursor, sid, snapshot, sup_date, now)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return {"success": True, "session_id": sid}
        except Exception as exc:
            logger.warning(
                "refresh_route_header failed (sid=%s): %s", sid, exc,
            )
            return {"success": False, "error": str(exc)}

    @with_db_retry
    def load_session_by_id(self, session_id: str) -> dict[str, Any] | None:
        """Load by exact session_id (vs load_session which collapses to most-recent on (route, date))."""
        if not self.available:
            return None
        try:
            with self._open_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"SELECT TOP 1 *, "
                    f"  CONVERT(VARCHAR(10), supervision_date, 120) AS sup_date_str "
                    f"FROM {self._s.route_summary_table} WHERE session_id = ?",
                    (session_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                cols = [d[0] for d in cursor.description]
                route = dict(zip(cols, row, strict=False))
                rcode = route.get("route_code", "")
                date = route.get("sup_date_str", "")
                custs = self._fetch_all(cursor, self._s.customer_summary_table, session_id)
                items = self._fetch_all(cursor, self._s.item_details_table, session_id)
                return self._reconstruct(session_id, rcode, date, route, custs, items)
        except Exception as exc:
            logger.error("DB load_session_by_id failed for %s: %s", session_id, exc)
            return None

    @with_db_retry
    def load_session(self, route_code: str, date: str) -> dict[str, Any] | None:
        if not self.available:
            return None
        try:
            with self._open_conn() as conn:
                cursor = conn.cursor()

                # Route header: most-recent row for (route, date); customers/items use canonical aggregation.
                cursor.execute(
                    f"SELECT TOP 1 * FROM {self._s.route_summary_table} "
                    f"WHERE route_code = ? AND supervision_date = ? "
                    f"ORDER BY session_started_at DESC",
                    (route_code, date),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                cols = [d[0] for d in cursor.description]
                route = dict(zip(cols, row, strict=False))
                # Canonical sid; legacy {route}_{date}_{ts}_{uuid} rows still surface via the aggregation read below.
                session_id = f"{route_code}_{date}"

                custs, items = self._fetch_canonical_rows(
                    cursor, route_code, date, visited_only=False,
                )
                return self._reconstruct(session_id, route_code, date, route, custs, items)
        except Exception as exc:
            logger.error("DB load failed for %s/%s: %s", route_code, date, exc)
            return None

    # Per-LLM-payload saves: each LLM artifact has its own UPDATE path; per-visit upserts never touch LLM columns.

    @with_db_retry
    def _list_customer_codes_with(
        self, route_code: str, date: str, where_clause: str,
    ) -> set[str]:
        """Distinct customer_codes matching ``where_clause`` across all session_ids for (route, date).

        Aggregating across session_ids covers legacy {route}_{date}_{ts}_{uuid} sids.
        ``where_clause`` is a trusted in-module literal; only route_code/date are bound.
        """
        if not self.available:
            return set()
        try:
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"SELECT DISTINCT cs.customer_code "
                    f"FROM {self._s.customer_summary_table} cs "
                    f"INNER JOIN {self._s.route_summary_table} rs "
                    f"   ON rs.session_id = cs.session_id "
                    f"WHERE rs.route_code = ? AND rs.supervision_date = ? "
                    f"  AND cs.customer_code IS NOT NULL "
                    f"  AND {where_clause}",
                    (route_code, date),
                )
                return {str(r[0]) for r in cur.fetchall() if r and r[0]}
        except Exception as exc:
            logger.warning(
                "supervision idempotency query failed for %s/%s (%s): %s",
                route_code, date, where_clause, exc,
            )
            return set()




    @with_db_retry
    def repair_supervision_day(self, date: str) -> dict[str, Any]:
        """Repair supervision rows whose recs were missing at write time.

        Single-transaction backfill that resolves the rec-timing race:
          1. UPDATE yf_supervision_items: fill original_recommended_qty +
             adjusted_recommended_qty + item_name from yf_recommended_orders
             for rows where original_recommended_qty=0 and the item exists
             in the canonical rec table for the same (route, date, customer,
             item). Untouched rows (already correct, or genuinely unplanned
             drop-ins) stay as-is.
          2. UPDATE yf_supervision_customers: recompute qty_recommended +
             skus_recommended + sku_coverage_rate + customer_accuracy_avg +
             customer_performance_score + qty_fulfillment_rate from the
             now-repaired item rows.
          3. UPDATE yf_supervision_routes: recompute planned_qty_recommended,
             visited_qty_recommended, qty_fulfillment_rate, route_performance_score
             from the now-repaired customer rows.

        Idempotent: re-running on already-correct data is a no-op.
        Returns per-stage rowcounts so the cascade can log them.
        """
        if not self.available:
            return {"success": False, "error": "DB not configured"}
        items_t = self._s.item_details_table
        cust_t  = self._s.customer_summary_table
        route_t = self._s.route_summary_table
        recs_t  = self._resolve_recommendations_table()
        try:
            with self._open_conn() as conn:
                cur = conn.cursor()
                try:
                    # Stage 0: INSERT missing recommended items.
                    # supervision_items only has rows for items that were actually
                    # purchased; items recommended-but-not-sold are absent entirely.
                    # For correct qty_recommended scoring we need to materialise those
                    # missing rows with actual_qty=0, was_item_sold=false.
                    cur.execute(f"""
                        INSERT INTO {items_t} (
                            session_id, customer_code, item_code, item_name,
                            original_recommended_qty, adjusted_recommended_qty,
                            actual_qty, was_manually_edited, was_item_sold,
                            recommendation_tier, priority_score,
                            purchase_frequency_pct, purchase_cycle_days, days_since_last_purchase,
                            van_inventory_qty, record_saved_at
                        )
                        SELECT rh.session_id, r.customer_code, r.item_code, r.item_name,
                               r.recommended_quantity, r.recommended_quantity,
                               0, 0, 0,
                               r.tier, r.priority_score,
                               r.frequency_percent, r.purchase_cycle_days, r.days_since_last_purchase,
                               COALESCE(r.van_load, 0), SYSUTCDATETIME()
                        FROM {recs_t}  r
                        JOIN {route_t} rh
                          ON rh.route_code        = r.route_code
                         AND rh.supervision_date  = r.trx_date
                        WHERE r.trx_date = ?
                          AND r.recommended_quantity > 0
                          AND NOT EXISTS (
                              SELECT 1 FROM {items_t} i
                              WHERE i.session_id    = rh.session_id
                                AND i.customer_code = r.customer_code
                                AND i.item_code     = r.item_code
                          )
                    """, date)
                    items_inserted = cur.rowcount if cur.rowcount is not None else 0

                    # Ensure the parent customer row also exists for items we just inserted.
                    # Without this, INSERT would orphan items against the customer rollups.
                    cur.execute(f"""
                        INSERT INTO {cust_t} (
                            session_id, customer_code, customer_name,
                            visit_sequence, skus_recommended, skus_sold, sku_coverage_rate,
                            qty_recommended, qty_actual, customer_accuracy_avg,
                            customer_performance_score, record_saved_at
                        )
                        SELECT rh.session_id, r.customer_code,
                               MAX(r.customer_name), 0,
                               COUNT(*), 0, 0,
                               SUM(r.recommended_quantity), 0, 0, 0, SYSUTCDATETIME()
                        FROM {recs_t}  r
                        JOIN {route_t} rh
                          ON rh.route_code        = r.route_code
                         AND rh.supervision_date  = r.trx_date
                        WHERE r.trx_date = ?
                          AND r.recommended_quantity > 0
                          AND NOT EXISTS (
                              SELECT 1 FROM {cust_t} c
                              WHERE c.session_id    = rh.session_id
                                AND c.customer_code = r.customer_code
                          )
                        GROUP BY rh.session_id, r.customer_code
                    """, date)
                    customers_inserted = cur.rowcount if cur.rowcount is not None else 0

                    # Stage 1: heal items from canonical rec table.
                    cur.execute(f"""
                        UPDATE i
                        SET   i.original_recommended_qty = r.recommended_quantity,
                              i.adjusted_recommended_qty = CASE
                                  WHEN i.adjusted_recommended_qty IS NULL
                                    OR i.adjusted_recommended_qty = 0
                                    OR i.adjusted_recommended_qty = i.original_recommended_qty
                                  THEN r.recommended_quantity
                                  ELSE i.adjusted_recommended_qty
                              END,
                              i.item_name = CASE
                                  WHEN i.item_name IS NULL OR i.item_name = ''
                                  THEN r.item_name
                                  ELSE i.item_name
                              END,
                              i.recommendation_tier = CASE
                                  WHEN i.recommendation_tier IS NULL
                                    OR i.recommendation_tier = ''
                                    OR i.recommendation_tier = 'UNPLANNED'
                                  THEN r.tier
                                  ELSE i.recommendation_tier
                              END,
                              i.purchase_frequency_pct = CASE
                                  WHEN i.purchase_frequency_pct IS NULL OR i.purchase_frequency_pct = 0
                                  THEN r.frequency_percent
                                  ELSE i.purchase_frequency_pct
                              END,
                              i.purchase_cycle_days = CASE
                                  WHEN i.purchase_cycle_days IS NULL OR i.purchase_cycle_days = 0
                                  THEN r.purchase_cycle_days
                                  ELSE i.purchase_cycle_days
                              END,
                              i.days_since_last_purchase = CASE
                                  WHEN i.days_since_last_purchase IS NULL OR i.days_since_last_purchase = 0
                                  THEN r.days_since_last_purchase
                                  ELSE i.days_since_last_purchase
                              END
                        FROM {items_t} i
                        JOIN {route_t} rh ON rh.session_id = i.session_id
                        JOIN {recs_t} r
                          ON r.route_code    = rh.route_code
                         AND r.trx_date      = rh.supervision_date
                         AND r.customer_code = i.customer_code
                         AND r.item_code     = i.item_code
                        WHERE rh.supervision_date = ?
                          AND (i.original_recommended_qty IS NULL OR i.original_recommended_qty = 0)
                          AND r.recommended_quantity > 0
                    """, date)
                    items_repaired = cur.rowcount if cur.rowcount is not None else 0

                    # Stage 2: recompute per-customer rollups from healed items.
                    # Computed columns (qty_fulfillment_rate) auto-recompute from
                    # qty_recommended + qty_actual, so we don't set them here.
                    cur.execute(f"""
                        UPDATE c
                        SET   c.qty_recommended    = agg.sum_rec,
                              c.skus_recommended   = agg.skus_rec,
                              c.sku_coverage_rate  = CASE WHEN agg.skus_rec > 0
                                  THEN (agg.skus_sold * 100.0) / agg.skus_rec ELSE 0 END,
                              c.customer_accuracy_avg = CASE WHEN agg.sum_rec > 0
                                  THEN CASE WHEN (CAST(c.qty_actual AS FLOAT) * 100.0) / agg.sum_rec > 100.0 THEN 100.0 ELSE (CAST(c.qty_actual AS FLOAT) * 100.0) / agg.sum_rec END
                                  ELSE 0 END,
                              c.customer_performance_score = CASE WHEN agg.sum_rec > 0
                                  THEN CASE WHEN (CAST(c.qty_actual AS FLOAT) * 100.0) / agg.sum_rec > 100.0 THEN 100.0 ELSE (CAST(c.qty_actual AS FLOAT) * 100.0) / agg.sum_rec END
                                  ELSE 0 END
                        FROM {cust_t} c
                        JOIN {route_t} rh ON rh.session_id = c.session_id
                        CROSS APPLY (
                            SELECT SUM(i.original_recommended_qty) AS sum_rec,
                                   COUNT(CASE WHEN i.original_recommended_qty > 0 THEN 1 END) AS skus_rec,
                                   COUNT(CASE WHEN i.original_recommended_qty > 0
                                              AND i.was_item_sold = 1 THEN 1 END) AS skus_sold
                            FROM {items_t} i
                            WHERE i.session_id = c.session_id
                              AND i.customer_code = c.customer_code
                        ) agg
                        WHERE rh.supervision_date = ?
                    """, date)
                    customers_repaired = cur.rowcount if cur.rowcount is not None else 0

                    # Stage 3: recompute per-route rollups from healed customers.
                    # customer_completion_rate + qty_fulfillment_rate are computed
                    # columns on the route table -- they recompute from the base
                    # columns automatically.
                    cur.execute(f"""
                        UPDATE r
                        SET   r.planned_qty_recommended    = agg.planned_rec,
                              r.visited_qty_recommended    = agg.visited_rec,
                              r.visited_qty_actual         = agg.visited_act,
                              r.route_performance_score    = CASE WHEN agg.visited_rec > 0
                                  THEN CASE WHEN (CAST(agg.visited_act AS FLOAT) * 100.0) / agg.visited_rec > 100.0
                                            THEN 100.0
                                            ELSE (CAST(agg.visited_act AS FLOAT) * 100.0) / agg.visited_rec END
                                  ELSE 0 END,
                              r.customers_planned          = agg.planned_count,
                              r.customers_visited          = agg.visited_count
                        FROM {route_t} r
                        CROSS APPLY (
                            SELECT
                                SUM(c.qty_recommended)                                                  AS planned_rec,
                                SUM(CASE WHEN c.visit_sequence > 0 THEN c.qty_recommended ELSE 0 END)  AS visited_rec,
                                SUM(CASE WHEN c.visit_sequence > 0 THEN c.qty_actual      ELSE 0 END)  AS visited_act,
                                COUNT(*)                                                                AS planned_count,
                                SUM(CASE WHEN c.visit_sequence > 0 THEN 1 ELSE 0 END)                  AS visited_count
                            FROM {cust_t} c
                            WHERE c.session_id = r.session_id
                        ) agg
                        WHERE r.supervision_date = ?
                    """, date)
                    routes_repaired = cur.rowcount if cur.rowcount is not None else 0

                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return {
                "success": True,
                "date": date,
                "items_inserted":     int(items_inserted),
                "customers_inserted": int(customers_inserted),
                "items_repaired":     int(items_repaired),
                "customers_repaired": int(customers_repaired),
                "routes_repaired":    int(routes_repaired),
            }
        except Exception as exc:
            logger.error("repair_supervision_day failed for date=%s: %s", date, exc)
            return {"success": False, "error": str(exc)}

    def _resolve_recommendations_table(self) -> str:
        """Return fully-qualified recs table. SS settings store the bare name
        ('yf_recommended_orders' by default); prepend [database].[dbo] using
        the SS DB to match other DML in this module."""
        raw = (self._s.recommendations_table or "yf_recommended_orders").strip()
        if raw.startswith("["):
            return raw
        db = getattr(self._s.db, "database", "") or "YaumiAIML"
        return f"[{db}].[dbo].[{raw}]"



    @with_db_retry
    def load_session_visits(
        self, route_code: str, date: str,
        *, include_redistributions: bool = False,
    ) -> dict[str, Any] | None:
        """Saved per-customer visit data for a (route, date); shape matches /session/visit output.

        ``include_redistributions=True`` adds the per-visit redistribution-replay view
        (~2-3 s on a 30-customer route); production callers pass False and use
        ``load_redistribution_for_customer`` for the drill-in.
        """
        if not self.available:
            return None
        # TTL cache absorbs /session/saved polling; TTL is half-cron-cadence so cron writes show within a tick.
        cache_ttl = self._s.effective_saved_visits_cache_ttl
        cache_key = (str(route_code), str(date), bool(include_redistributions))
        if cache_ttl > 0:
            now = time.monotonic()
            with self._saved_visits_cache_lock:
                entry = self._saved_visits_cache.get(cache_key)
                if entry is not None and (now - entry[0]) < cache_ttl:
                    return entry[1]
        # Connection held only for SQL fetches; _build_visits_payload runs OUTSIDE to release shared locks.
        try:
            with self._open_conn() as conn:
                cursor = conn.cursor()
                # Latest writer wins for route header; customers/items aggregate across session_ids.
                # visited_only filter: visit_sequence>0 AND qty_recommended>0 (planned visits only).
                cursor.execute(
                    f"SELECT TOP 1 * FROM {self._s.route_summary_table} "
                    f"WHERE route_code = ? AND supervision_date = ? "
                    f"ORDER BY session_started_at DESC",
                    (route_code, date),
                )
                rh_row = cursor.fetchone()
                if not rh_row:
                    # Cache the None so polls don't re-query an empty route.
                    if cache_ttl > 0:
                        with self._saved_visits_cache_lock:
                            self._saved_visits_cache[cache_key] = (
                                time.monotonic(), None,
                            )
                    return None
                rh_cols = [d[0] for d in cursor.description]
                route_header = dict(zip(rh_cols, rh_row, strict=False))

                custs, items = self._fetch_canonical_rows(
                    cursor, route_code, date, visited_only=True,
                )
                # The full canonical set + the per-visit redistribution
                # replay are the costliest piece of the saved-visits
                # build. They're only needed when a supervisor opens
                # the per-customer drill-in. Polling callers pass
                # ``include_redistributions=False`` so the periodic
                # refresh stays cheap; drill-in hits the dedicated
                # ``/session/redistribution/{sid}/{customer}`` endpoint.
                if include_redistributions:
                    all_custs, all_items = self._fetch_canonical_rows(
                        cursor, route_code, date, visited_only=False,
                    )
                else:
                    all_custs, all_items = None, None
                # Pre-visit briefings for every planned customer
                # (visited + not-yet-visited). Pulled even on the cheap
                # poll path -- the dict is small and lets the FE render
                # any planned customer's briefing without a live LLM
                # call. Empty when the cron has not yet briefed any
                # customer on this route/date.
                briefings: dict[str, str] = {}  # LLM briefings are on-demand now, not pre-cached
                # Current recommendation snapshot, used downstream to
                # dedupe alsoBought against the live plan -- an item
                # that became recommended AFTER the visit was recorded
                # must not surface in both the planned table and the
                # alsoBought chip strip.
                current_plan = self._fetch_current_plan_items(
                    cursor, route_code, date,
                )
        except Exception as exc:
            logger.error(
                "DB load_session_visits failed for %s/%s: %s",
                route_code, date, exc,
            )
            return None

        # Connection closed; locks released. Replay engine runs in
        # pure Python on the already-fetched rows -- no DB contention.
        # Canonical sid: see ``load_session`` for the rationale. The
        # frontend does not key off this sid (it uses the one returned
        # by /initialize); emitting the canonical form keeps the wire
        # one consistent identifier per (route, date).
        sid = f"{route_code}_{date}"
        payload = self._build_visits_payload(
            sid, route_header, custs, items,
            all_custs=all_custs, all_items=all_items,
            include_redistributions=include_redistributions,
            current_plan=current_plan,
        )
        payload["briefings"] = briefings
        # Cache the full payload; readers are read-only so sharing the dict is safe.
        if cache_ttl > 0:
            with self._saved_visits_cache_lock:
                self._saved_visits_cache[cache_key] = (
                    time.monotonic(), payload,
                )
        return payload

    @with_db_retry
    def load_redistribution_for_customer(
        self, route_code: str, date: str, customer_code: str,
    ) -> dict[str, Any] | None:
        """Drill-in replay of a single customer's redistribution view; same wire shape as the hot path."""
        if not self.available:
            return None
        # Same connection-scope split as load_session_visits: replay runs outside the connection.
        try:
            with self._open_conn() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"SELECT TOP 1 * FROM {self._s.route_summary_table} "
                    f"WHERE route_code = ? AND supervision_date = ? "
                    f"ORDER BY session_started_at DESC",
                    (route_code, date),
                )
                rh_row = cursor.fetchone()
                if not rh_row:
                    return None
                rh_cols = [d[0] for d in cursor.description]
                route_header = dict(zip(rh_cols, rh_row, strict=False))
                custs, items = self._fetch_canonical_rows(
                    cursor, route_code, date, visited_only=True,
                )
                all_custs, all_items = self._fetch_canonical_rows(
                    cursor, route_code, date, visited_only=False,
                )
                current_plan = self._fetch_current_plan_items(
                    cursor, route_code, date,
                )
        except Exception as exc:
            logger.error(
                "DB load_redistribution_for_customer failed for %s/%s/%s: %s",
                route_code, date, customer_code, exc,
            )
            return None

        sid = f"{route_code}_{date}"
        full = self._build_visits_payload(
            sid, route_header, custs, items,
            all_custs=all_custs, all_items=all_items,
            include_redistributions=True,
            current_plan=current_plan,
        )
        visit = (full.get("visits") or {}).get(str(customer_code))
        return visit.get("redistributions") if visit else None

    @staticmethod
    def _build_visits_payload(
        sid: str, route_header: dict, custs: list[dict], items: list[dict],
        *,
        all_custs: list[dict] | None = None,
        all_items: list[dict] | None = None,
        include_redistributions: bool = True,
        current_plan: dict[str, frozenset[str]] | None = None,
    ) -> dict[str, Any]:
        """Translate stored rows into the visit-result shape the live UI consumes.

        ``all_custs``/``all_items`` (full canonical set) drive the per-visit redistribution
        replay; omitted -> safe-default empty redistributions view.
        """
        # actuals = all invoiced items; also_bought = off-plan subset (rec=0, actual>0).
        actuals_by_cust: dict[str, dict[str, int]] = {}
        also_bought_by_cust: dict[str, list[dict[str, Any]]] = {}
        _current_plan: dict[str, frozenset[str]] = current_plan or {}
        for it in items:
            cc = str(it.get("customer_code", ""))
            if not cc:
                continue
            ic = str(it.get("item_code", ""))
            if not ic:
                continue
            qty_act = int(it.get("actual_qty") or 0)
            actuals_by_cust.setdefault(cc, {})[ic] = qty_act
            rec_qty = int(it.get("original_recommended_qty") or 0)
            # alsoBought = off-plan at visit time AND still off-plan now (avoids double-listing
            # an item that became recommended after the visit landed).
            if (
                rec_qty == 0
                and qty_act > 0
                and ic not in _current_plan.get(cc, frozenset())
            ):
                also_bought_by_cust.setdefault(cc, []).append({
                    "item_code": ic,
                    "qty": qty_act,
                })
        for _cc, rows in also_bought_by_cust.items():
            rows.sort(key=lambda r: r["qty"], reverse=True)

        # One-pass redistribution replay; local imports break the api.schemas <-> core cycle.
        redistributions_by_cust: dict[str, dict[str, Any]] = {}
        try:
            if not include_redistributions:
                # Periodic-poll path skips the replay; drill-in uses compute_redistribution_for_customer.
                raise _SkipReplay
            from sales_supervision.core.redistribution import (
                compute_redistributions_for_saved_visits,
            )
            from sales_supervision.models.schemas import (
                ScoreResult,
            )
            from sales_supervision.models.schemas import (
                Session as _Session,
            )
            from sales_supervision.models.schemas import (
                SessionCustomer as _SessionCustomer,
            )
            from sales_supervision.models.schemas import (
                SessionItem as _SessionItem,
            )

            # Minimal in-memory Session for the shaper; preserves tier so the planned/unplanned classifier matches the live path.
            roster_custs = all_custs if all_custs is not None else custs
            roster_items = all_items if all_items is not None else items
            items_by_cust_for_replay: dict[str, list[_SessionItem]] = {}
            for it in roster_items:
                cc = str(it.get("customer_code", ""))
                if not cc:
                    continue
                ic = str(it.get("item_code", ""))
                if not ic:
                    continue
                # van_inventory_qty must round-trip so the ledger's spare-van math matches live.
                items_by_cust_for_replay.setdefault(cc, []).append(_SessionItem(
                    item_code=ic,
                    item_name=str(it.get("item_name") or ""),
                    recommended_qty=int(it.get("original_recommended_qty") or 0),
                    actual_qty=int(it.get("actual_qty") or 0),
                    was_sold=bool(it.get("was_item_sold") or False),
                    tier=str(it.get("recommendation_tier") or ""),
                    van_inventory_qty=int(it.get("van_inventory_qty") or 0),
                ))
            roster: dict[str, _SessionCustomer] = {}
            for c in roster_custs:
                cc = str(c.get("customer_code", ""))
                if not cc:
                    continue
                seq = int(c.get("visit_sequence") or 0)
                roster[cc] = _SessionCustomer(
                    customer_code=cc,
                    customer_name=str(c.get("customer_name") or ""),
                    items=items_by_cust_for_replay.get(cc, []),
                    visited=seq > 0,
                    visit_sequence=seq,
                    score=ScoreResult(
                        score=_to_float(c.get("customer_performance_score")),
                        coverage=_to_float(c.get("sku_coverage_rate")),
                        accuracy=_to_float(c.get("customer_accuracy_avg")),
                    ),
                )
            replay_session = _Session(
                session_id=sid,
                route_code=str(route_header.get("route_code") or ""),
                date=str(route_header.get("supervision_date") or ""),
                customers=roster,
            )

            # Walk visited planned customers in (seq, code) order; drop-ins ride on /session/unplanned.
            from sales_supervision.core.constants import is_unplanned_customer

            visited_codes = [
                cc for cc, cust in roster.items()
                if cust.visited and not is_unplanned_customer(cust)
            ]
            visited_codes.sort(key=lambda cc: (
                int(roster[cc].visit_sequence or 0), cc,
            ))
            views = compute_redistributions_for_saved_visits(
                replay_session, visited_codes,
            )
            for cc, view in views.items():
                redistributions_by_cust[cc] = view.model_dump()
        except _SkipReplay:
            pass
        except Exception as exc:
            # Safe default: empty redistributions; traceback captured for diagnosis.
            logger.exception(
                "saved-visits redistribution replay failed (sid=%s): %s",
                sid, exc,
            )

        visits: dict[str, dict[str, Any]] = {}
        for c in custs:
            cc = str(c.get("customer_code", ""))
            if not cc:
                continue
            visits[cc] = {
                "score": {
                    "score":    _to_float(c.get("customer_performance_score")),
                    "coverage": _to_float(c.get("sku_coverage_rate")),
                    "accuracy": _to_float(c.get("customer_accuracy_avg")),
                },
                "actualSales":      actuals_by_cust.get(cc, {}),
                "totalActual":      _to_int(c.get("qty_actual")),
                "totalRecommended": _to_int(c.get("qty_recommended")),
                "preVisitBriefing":  None,  # generated on-demand by the webapp
                "customerAnalysis":  None,  # generated on-demand by the webapp
                # Replayed view or Pydantic-built empty default.
                "redistributions":   redistributions_by_cust.get(
                    cc, _empty_redistribution_dict(),
                ),
                "alsoBought":        also_bought_by_cust.get(cc, []),
            }

        # Visit totals via the shared aggregator; /saved is planned-only so unplanned_visited_count=0.
        from sales_supervision.core.visit_totals import aggregate_visit_totals
        visit_totals = aggregate_visit_totals(
            planned_visited=[
                {
                    "total_actual": int(v["totalActual"]),
                    "total_recommended": int(v["totalRecommended"]),
                    "score": float(v["score"]["score"]),
                }
                for v in visits.values()
            ],
            unplanned_visited_count=0,
        )

        return {
            "available": True,
            "session_id": sid,
            "visits": visits,
            "routeAnalysis": None,  # generated on-demand by the webapp
            "visit_totals": visit_totals,
        }

    @with_db_retry
    def check_exists(self, route_code: str, date: str) -> bool:
        if not self.available:
            return False
        try:
            with self._open_conn() as conn:
                cursor = conn.cursor()
                # TOP 1 1 short-circuits via the (route_code, supervision_date) index.
                cursor.execute(
                    f"SELECT TOP 1 1 FROM {self._s.route_summary_table} "
                    f"WHERE route_code = ? AND supervision_date = ?",
                    (route_code, date),
                )
                return cursor.fetchone() is not None
        except Exception as exc:
            logger.warning("DB exists-check failed for %s/%s: %s", route_code, date, exc)
            return False

    # ------------------------------------------------------------------
    # Write helpers (per-visit upsert)
    # ------------------------------------------------------------------

    def _upsert_route_header(
        self, cursor: Any, sid: str, snapshot: dict[str, Any],
        sup_date: str, now: datetime,
    ) -> None:
        """Atomic MERGE upsert for the route header row.

        Three qty totals: planned_qty_recommended (all journey-plan),
        visited_qty_recommended (subset flowed to visited), visited_qty_actual.
        customer_completion_rate / qty_fulfillment_rate are PERSISTED columns.
        ``session_started_at`` is bound here for the INSERT branch; the
        MERGE's UPDATE set excludes it so the stamp survives across
        every later upsert.
        """
        table = self._s.route_summary_table
        clipped_sid = _str_clip(sid, _LEN_SESSION_ID)
        rcode  = _str_clip(snapshot.get("routeCode"), _LEN_ROUTE_CODE)
        # Planned-only counts; legacy keys retained for hand-crafted snapshots.
        cust_planned = _to_int(snapshot.get("plannedCustomers",
                                            snapshot.get("totalCustomers")))
        cust_visited = _to_int(snapshot.get("plannedVisitedCustomers",
                                            snapshot.get("visitedCustomers")))
        planned_rec  = _to_int(snapshot.get("totalRecommended"))
        visited_rec  = _to_int(snapshot.get("visitedRecommended"))
        visited_act  = _to_int(snapshot.get("visitedActual"))
        perf_score   = _clamp_score(snapshot.get("visitedAchievement"))

        cursor.execute(
            _ROUTE_MERGE_TPL.format(table=table),
            (
                clipped_sid, rcode, sup_date,
                cust_planned, cust_visited,
                planned_rec, visited_rec, visited_act,
                perf_score,
                _STATUS_ACTIVE,
                now,                       # session_started_at  -- INSERT only
                None,                      # session_completed_at
            ),
        )

    def _upsert_customer(
        self, cursor: Any, sid: str, customer_code: str,
        customer: dict[str, Any], now: datetime,
    ) -> None:
        """Atomic MERGE upsert for one customer row.

        qty_fulfillment_rate is PERSISTED. LLM columns are bound NULL on INSERT and
        excluded from UPDATE so the dedicated LLM save paths own them exclusively.
        """
        table = self._s.customer_summary_table
        clipped_sid  = _str_clip(sid, _LEN_SESSION_ID)
        clipped_cust = _str_clip(customer_code, _LEN_CUSTOMER_CODE)
        cname        = _str_clip(customer.get("customerName"), _LEN_CUSTOMER_NAME)
        visit_seq    = _to_int(customer.get("visitSequence"))
        total_items  = _to_int(customer.get("totalItems", len(customer.get("items", []))))
        sold         = _to_int(customer.get("itemsSold"))
        coverage     = _clamp_score(customer.get("coverage"))
        total_rec    = _to_int(customer.get("totalRecommended"))
        total_act    = _to_int(customer.get("totalActual"))
        accuracy     = _clamp_score(customer.get("accuracy"))
        score        = _clamp_score(customer.get("score"))

        cursor.execute(
            _CUSTOMER_MERGE_TPL.format(table=table),
            (
                clipped_sid, clipped_cust,
                cname, visit_seq,
                total_items, sold, coverage,
                total_rec, total_act,
                accuracy, score,
                now,               # record_saved_at
            ),
        )

    def _replace_customer_items(
        self, cursor: Any, sid: str, customer_code: str,
        customer: dict[str, Any], now: datetime,
    ) -> int:
        """DELETE prior item rows + INSERT current set, scoped to (session_id, customer_code).

        recommendation_adjustment is PERSISTED so it's excluded from the INSERT tuple.
        """
        table = self._s.item_details_table
        clipped_sid  = _str_clip(sid, _LEN_SESSION_ID)
        clipped_cust = _str_clip(customer_code, _LEN_CUSTOMER_CODE)

        cursor.execute(_ITEMS_DELETE_TPL.format(table=table), (clipped_sid, clipped_cust))

        rows = []
        for it in customer.get("items", []):
            rec_qty = _to_int(it.get("RecommendedQuantity"))
            # Prefer stored EffectiveRecommended; fall back to rec + adjustment for missing/null fields.
            effective = _to_int(
                it.get("EffectiveRecommended"),
                default=rec_qty + _to_int(it.get("Adjustment")),
            )
            rows.append((
                clipped_sid,
                clipped_cust,
                _str_clip(it.get("ItemCode"), _LEN_ITEM_CODE),
                _str_clip(it.get("ItemName"), _LEN_ITEM_NAME),
                rec_qty, effective,
                _to_int(it.get("ActualQuantity")),
                1 if it.get("WasEdited") else 0,
                1 if it.get("WasSold") else 0,
                _str_clip(it.get("Tier"), _LEN_TIER),
                # priority_score: DECIMAL(10,2); _clamp_score fits.
                _clamp_score(it.get("PriorityScore")),
                _to_int(it.get("VanLoad")),
                _to_int(it.get("DaysSinceLastPurchase")),
                _clamp_score(it.get("PurchaseCycleDays")),
                # purchase_frequency_pct: DECIMAL(8,2).
                _clamp_score(it.get("FrequencyPercent")),
                now,
            ))

        if rows:
            # Chunk to bound worst-case batch size.
            chunk = max(int(getattr(self._s.db, "executemany_chunk_size", 1000)), 1)
            tpl = _ITEM_INSERT_TPL.format(table=table)
            for i in range(0, len(rows), chunk):
                cursor.executemany(tpl, rows[i:i + chunk])
        return len(rows)

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def _fetch_all(self, cursor: Any, table: str, sid: str) -> list[dict]:
        """Schema-driven read of every row for a session_id; ORDER BY id pins insertion order."""
        cursor.execute(
            f"SELECT * FROM {table} WHERE session_id = ? ORDER BY id",
            (sid,),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row, strict=False)) for row in cursor.fetchall()]

    def _fetch_canonical_rows(
        self,
        cursor: Any,
        route_code: str,
        date: str,
        *,
        visited_only: bool,
    ) -> tuple[list[dict], list[dict]]:
        """Per-(route, date) customer + item rows deduped across legacy parallel session_ids.

        Dedup rule: per customer_code, keep the row with most-recent record_saved_at (id tiebreak).
        Items match the picked customer's (session_id, customer_code) so they stay paired with
        their producing write. ``visited_only`` restricts to planned-visited
        (visit_sequence > 0 AND qty_recommended > 0).
        """
        extra = (
            "AND cs.visit_sequence > 0 AND cs.qty_recommended > 0"
            if visited_only else ""
        )
        cust_sql = f"""
            ;WITH route_sessions AS (
                SELECT session_id
                FROM {self._s.route_summary_table}
                WHERE route_code = ? AND supervision_date = ?
            ),
            ranked AS (
                SELECT cs.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY cs.customer_code
                           ORDER BY cs.record_saved_at DESC, cs.id DESC
                       ) AS rn
                FROM {self._s.customer_summary_table} cs
                INNER JOIN route_sessions rs ON rs.session_id = cs.session_id
                WHERE cs.customer_code IS NOT NULL
                  {extra}
            )
            SELECT * FROM ranked WHERE rn = 1
            ORDER BY visit_sequence, id
        """
        cursor.execute(cust_sql, (route_code, date))
        cols = [d[0] for d in cursor.description]
        custs = [
            {k: v for k, v in zip(cols, row, strict=False) if k != "rn"}
            for row in cursor.fetchall()
        ]
        if not custs:
            return [], []

        # Items matched to picked (session_id, customer_code) so old-session rows don't bleed in.
        item_sql = f"""
            ;WITH route_sessions AS (
                SELECT session_id
                FROM {self._s.route_summary_table}
                WHERE route_code = ? AND supervision_date = ?
            ),
            ranked AS (
                SELECT cs.session_id, cs.customer_code,
                       ROW_NUMBER() OVER (
                           PARTITION BY cs.customer_code
                           ORDER BY cs.record_saved_at DESC, cs.id DESC
                       ) AS rn
                FROM {self._s.customer_summary_table} cs
                INNER JOIN route_sessions rs ON rs.session_id = cs.session_id
                WHERE cs.customer_code IS NOT NULL
                  {extra}
            ),
            picked AS (
                SELECT session_id, customer_code FROM ranked WHERE rn = 1
            )
            SELECT it.* FROM {self._s.item_details_table} it
            INNER JOIN picked p
                ON p.session_id = it.session_id
               AND p.customer_code = it.customer_code
            ORDER BY it.id
        """
        cursor.execute(item_sql, (route_code, date))
        cols = [d[0] for d in cursor.description]
        items = [dict(zip(cols, row, strict=False)) for row in cursor.fetchall()]
        return custs, items


    def _fetch_current_plan_items(
        self, cursor: Any, route_code: str, date: str,
    ) -> dict[str, frozenset]:
        """Snapshot of the CURRENT recommendation set for this (route,
        date), keyed by ``customer_code -> frozenset(item_code)``.

        Used to dedupe ``alsoBought`` against the live plan: an item
        that was off-plan when the rep marked the visit but has since
        been added to the recommendation (typical when an inter-day
        regeneration runs after the supervision session was created)
        must not appear in BOTH the planned table (sourced from current
        recs) and the alsoBought chips (sourced from the visit-time
        snapshot in supervision_items). The dedupe layers the current
        plan on top so the supervisor sees one consistent classification
        per item.

        Bounded query: ~items-per-customer x customers-per-route rows
        (~500 for a typical route). Reads in the same transaction as
        the supervision rows so locks coalesce; the cursor's shared
        locks already span the route's data, so the marginal cost is
        the network round-trip only.
        """
        # Use the resolved FQN (database.dbo.table) -- the bare name relied
        # on the cursor's current-DB context matching the recs table's host
        # DB. Symmetric with repair_supervision_day so a future cross-DB
        # placement of the recs table can't silently break this read.
        sql = (
            f"SELECT customer_code, item_code FROM {self._resolve_recommendations_table()} "
            f"WHERE trx_date = ? AND route_code = ?"
        )
        cursor.execute(sql, (date, route_code))
        result: dict[str, set] = {}
        for code, item in cursor.fetchall():
            if not code or not item:
                continue
            result.setdefault(str(code), set()).add(str(item))
        return {k: frozenset(v) for k, v in result.items()}

    # ------------------------------------------------------------------
    # Reconstruct session from DB rows
    # ------------------------------------------------------------------

    @staticmethod
    def _reconstruct(sid: str, route_code: str, date: str, route: dict, custs: list[dict], items: list[dict]) -> dict:
        """Rebuild session JSON from DB rows; every key matches live (pre-save) semantics.

        RecommendedQuantity = engine ORIGINAL (adjusted value rides on EffectiveRecommended);
        accuracy = mean per-item accuracy (qty ratio is qtyFulfillmentRate).
        Explainability fields (WhyItem/WhyQuantity/Confidence/...) aren't persisted; modal shows "-".
        """
        items_by_cust: dict[str, list] = {}
        for it in items:
            cc = str(it.get("customer_code", ""))
            items_by_cust.setdefault(cc, []).append({
                "ItemCode": it.get("item_code", ""),
                "ItemName": it.get("item_name", ""),
                "RecommendedQuantity":  it.get("original_recommended_qty", 0),
                "EffectiveRecommended": it.get("adjusted_recommended_qty", 0),
                "ActualQuantity":       it.get("actual_qty", 0),
                "Adjustment":           it.get("recommendation_adjustment", 0),
                "WasSold":              bool(it.get("was_item_sold", 0)),
                "WasEdited":            bool(it.get("was_manually_edited", 0)),
                "Tier":                 it.get("recommendation_tier", ""),
                "PriorityScore":        it.get("priority_score", 0),
                "DaysSinceLastPurchase": it.get("days_since_last_purchase", 0),
                "PurchaseCycleDays":    it.get("purchase_cycle_days", 0),
                "FrequencyPercent":     it.get("purchase_frequency_pct", 0),
                "VanLoad":              it.get("van_inventory_qty", 0),
            })

        # ---- customers ----
        customers = {}
        for c in custs:
            cc = str(c.get("customer_code", ""))
            seq = int(c.get("visit_sequence") or 0)
            customers[cc] = {
                "customerCode":     cc,
                "customerName":     c.get("customer_name", "") or "",
                # visited = visit_sequence > 0; briefing-only rows (seq=0) must NOT be marked visited.
                "visited":          seq > 0,
                "visitSequence":    c.get("visit_sequence", 0),
                "score":            c.get("customer_performance_score", 0),
                "coverage":         c.get("sku_coverage_rate", 0),
                "accuracy":             c.get("customer_accuracy_avg", 0),
                "qtyFulfillmentRate":   c.get("qty_fulfillment_rate", 0),
                "totalRecommended":     c.get("qty_recommended", 0),
                "totalActual":          c.get("qty_actual", 0),
                "totalItems":           c.get("skus_recommended", 0),
                "itemsSold":            c.get("skus_sold", 0),
                "items":                items_by_cust.get(cc, []),
            }

        # Planned and visited-only totals exposed under distinct keys.
        return {
            "sessionId":             sid,
            "routeCode":             route_code,
            "date":                  date,
            "status":                route.get("session_status", "closed"),
            "totalCustomers":        route.get("customers_planned", 0),
            "visitedCustomers":      route.get("customers_visited", 0),
            "totalRecommended":      route.get("planned_qty_recommended", 0),
            "totalActual":           route.get("visited_qty_actual", 0),
            "visitedRecommended":    route.get("visited_qty_recommended", 0),
            "visitedActual":         route.get("visited_qty_actual", 0),
            "visitedAchievement":    route.get("route_performance_score", 0),
            "customers":             customers,
        }
