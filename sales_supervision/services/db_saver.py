"""
Per-visit upsert of supervision data into YaumiAIML (3 tables).
Column order matches scripts/create_tables.sql exactly.
YaumiLive is never written to.

Write model
-----------
Each ``process_visit`` call triggers ``upsert_visit(snapshot, customer_code)``
in the background. One transaction touches three tables:

  * yf_supervision_routes      -- one row per session, UPDATE-or-INSERT.
                                  Holds session totals that evolve with
                                  every visit.
  * yf_supervision_customers   -- UPDATE-or-INSERT for the visited customer
                                  (one row at a time, never the whole set).
  * yf_supervision_items       -- DELETE-where-(session, customer) +
                                  INSERT the customer's items. Items per
                                  customer are bounded (~10-20 rows), and
                                  the FK-by-session means deleting one
                                  customer's items never affects another's.

The pattern is idempotent on (session_id, customer_code): re-firing the
same visit converges on the same DB state, so a retry, a double-click,
or a visit-correction is safe.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

import pyodbc

# Cross-service import: the bounded AIML pool lives in
# demand_forecasting_pipeline so every YaumiAIML writer (this module,
# recommended_order/db_pusher, the demand-forecast pusher) shares one
# semaphore-bounded connection cap. The cross-service shape mirrors
# what auto_visit_service already does (it imports df settings for the
# cascade refresh) and what data/manager imports (recon engine), so no
# new dependency direction is introduced.
from demand_forecasting_pipeline.services.db_pool import get_pool
from sales_supervision.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# Column orders matching create_tables.sql.
#
# Excluded from the writer-side lists:
#   * ``id`` columns -- IDENTITY, auto-assigned by SQL Server.
#   * PERSISTED computed columns -- value derived from other columns,
#     not insertable. Owning the formula in one place (the schema)
#     prevents the writer / reader drift that "store the same number
#     twice" caused in earlier revisions.
#
# Computed columns (read-only on this side, present on SELECT *):
#   yf_supervision_routes.customer_completion_rate
#   yf_supervision_routes.qty_fulfillment_rate
#   yf_supervision_customers.qty_fulfillment_rate
#   yf_supervision_items.recommendation_adjustment
_ROUTE_COLS = [
    "session_id", "route_code", "supervision_date",
    "customers_planned", "customers_visited",
    "planned_qty_recommended", "visited_qty_recommended", "visited_qty_actual",
    "route_performance_score",
    "session_status", "session_started_at", "session_completed_at",
    # LLM payload column. Set NULL on first INSERT and only populated by
    # ``save_route_analysis``; the per-visit upsert never touches it so a
    # mid-session route review can land without racing the visit writer.
    "llm_route_analysis",
]

_CUSTOMER_COLS = [
    "session_id", "customer_code", "customer_name", "visit_sequence",
    "skus_recommended", "skus_sold", "sku_coverage_rate",
    "qty_recommended", "qty_actual",
    "customer_accuracy_avg", "customer_performance_score",
    # LLM payload columns. Both NULL on first INSERT; populated only by
    # ``save_pre_visit_briefing`` and ``save_customer_analysis`` so
    # per-visit upserts never overwrite them.
    "llm_pre_visit_briefing", "llm_performance_analysis",
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


def _insert_sql(table_placeholder: str, cols: List[str]) -> str:
    """Build the parametric INSERT template for a column list.

    The table name is left as a Python format slot (``{table}``) so the
    runtime can drop in the configured table name once -- the heavy
    string assembly (column list + placeholders) happens once at import.
    """
    col_list = ", ".join(f"[{c}]" for c in cols)
    placeholders = ", ".join("?" for _ in cols)
    return f"INSERT INTO {table_placeholder} ({col_list}) VALUES ({placeholders})"


def _merge_sql(
    table_placeholder: str,
    all_cols: List[str],
    update_cols: List[str],
    key_cols: List[str],
    *,
    lock_hints: str = "HOLDLOCK, UPDLOCK",
) -> str:
    """Build a single-row MERGE upsert template.

    Earlier this module used an UPDATE-then-INSERT pattern: ``UPDATE``
    by key, then ``INSERT`` if ``rowcount = 0``. Two concurrent writers
    on the first-ever visit for a (session, customer) would both find
    rowcount = 0 and both attempt INSERT -- one wins, the other trips
    ``uq_yf_sc`` and rolls back. After the deterministic ``session_id``
    rollout this race window widened (both writers target the same row
    instead of two parallel rows), and we observed noisy IntegrityError
    rollbacks when the auto-reconciler and a UI ``/visit`` raced on the
    same customer's first persistence.

    MERGE WITH (HOLDLOCK, UPDLOCK) replaces both legs with one atomic
    statement: the UPDLOCK upgrades the read lock to a write lock so
    the row-not-found scan can't race a concurrent insert, and HOLDLOCK
    extends the lock over the key range so a second writer waits until
    we commit instead of barreling past with a parallel INSERT. Same
    pattern the demand-forecast db_pusher already uses.

    ``all_cols`` is bound positionally as the ``VALUES`` clause; ``ON``
    keys are referenced from ``src.*`` so we only bind each value once.
    ``update_cols`` is the subset that mutates on a matched row -- LLM
    payload columns are intentionally excluded so a per-visit upsert
    can't overwrite a previously-saved briefing or analysis."""
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


# Mutable columns on a per-visit upsert. Excludes the natural key
# (session_id) and ``session_started_at`` -- the latter stamps the very
# first INSERT and is preserved across subsequent UPDATEs so the row
# always reflects "when this session opened".
_ROUTE_UPDATE_COLS = [
    "route_code", "supervision_date",
    "customers_planned", "customers_visited",
    "planned_qty_recommended", "visited_qty_recommended", "visited_qty_actual",
    "route_performance_score",
    "session_status", "session_completed_at",
]

# Mutable columns for a customer row on a per-visit upsert. Excludes the
# (session_id, customer_code) composite key AND the LLM payload columns:
# those are owned by the dedicated save paths (save_pre_visit_briefing /
# save_customer_analysis) and a per-visit UPDATE here would silently
# wipe a previously-saved LLM payload when the supervisor re-taps the
# customer. ``record_saved_at`` is refreshed each upsert so the row
# always points at the latest write.
_CUSTOMER_UPDATE_COLS = [
    "customer_name", "visit_sequence",
    "skus_recommended", "skus_sold", "sku_coverage_rate",
    "qty_recommended", "qty_actual",
    "customer_accuracy_avg", "customer_performance_score",
    "record_saved_at",
]

# Pre-built SQL templates -- one ``{table}`` slot, everything else
# fixed at module load. Saves rebuilding the same string on every visit.
#
# Route header + customer rows go through MERGE (atomic upsert with
# HOLDLOCK + UPDLOCK -- no UPDATE-then-INSERT race window). Item rows
# go through DELETE + INSERT scoped to (session_id, customer_code) --
# the whole item set is owned by one writer at a time so the race
# applies at the customer level, not per item.
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

def _to_int(value: Any, default: int = 0) -> int:
    """Best-effort int cast. Truncates floats (so 5.7 -> 5), maps None / NaN
    / unparseable strings to ``default``."""
    if value is None:
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        f = float(value)
        # NaN survives float() -- treat as missing.
        return f if f == f else default  # noqa: PLR0124 -- NaN check
    except (TypeError, ValueError):
        return default


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
    never trip a "String or binary data would be truncated" error."""
    if value is None:
        return ""
    s = str(value)
    return s if len(s) <= max_len else s[:max_len]


def _empty_redistribution_dict() -> Dict[str, Any]:
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


class DbSaver:
    """Pushes supervision session data to 3 DB tables in YaumiAIML."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        self._db = self._s.db

    @property
    def available(self) -> bool:
        return (
            self._db.configured
            and bool(self._s.route_summary_table)
            and bool(self._s.customer_summary_table)
            and bool(self._s.item_details_table)
        )

    def _pool(self):
        """Resolve (and lazily create) the shared AIML pool for this
        saver's connection string. Sized to accommodate concurrent
        BackgroundTask upserts (one per /visit fired in flight) plus
        the auto-visit reconciler tick -- floor of 4 leaves headroom
        for both. Pool sizing applies only on first creation of a
        given (conn_str, autocommit) key; later calls reuse the
        existing pool unchanged."""
        return get_pool(
            self._db.connection_string(),
            max_connections=4,
            connect_timeout=int(self._db.connection_timeout),
            query_timeout=int(self._db.query_timeout),
            autocommit=False,
        )

    @contextmanager
    def _open_conn(self) -> Iterator[pyodbc.Connection]:
        """Context manager that yields a pooled connection. The pool's
        ``acquire()`` already guarantees the connection is closed on
        exit (and the bounding semaphore released) so this wrapper only
        survives because callers reference ``self._open_conn()`` from
        many methods -- it now thin-wraps the pool's context."""
        with self._pool().acquire() as conn:
            yield conn

    # ------------------------------------------------------------------
    # Per-visit upsert
    # ------------------------------------------------------------------

    def upsert_visit(self, snapshot: Dict[str, Any], customer_code: str) -> Dict[str, Any]:
        """Push the route header + this one customer + their items.

        Called once per ``process_visit`` (typically as a FastAPI
        BackgroundTask so the response is not blocked on warehouse
        latency). One transaction across the three tables; rollback on
        any failure leaves the row set in its pre-call state.

        Idempotent on (session_id, customer_code) -- the same visit
        firing twice (retry, double-click, edit-and-resubmit) converges
        on the same row state.
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
        # ``supervision_date`` is NOT NULL DATE -- fall back to today's
        # date so a payload missing the field still lands the row.
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

    def refresh_route_header(self, snapshot: Dict[str, Any]) -> Dict[str, Any]:
        """Stamp the route header from ``snapshot`` without touching
        customer or item rows.

        Called at the top of every ``reconcile_route`` tick so the
        header columns (customers_planned, customers_visited,
        visited_qty_*, route_performance_score, etc.) always reflect
        the latest in-memory session state -- even on ticks where
        Phase 1 has nothing new to upsert and Phase 2 is fully
        idempotent. Without this hook a route that completed earlier
        in the day keeps stale counts forever (e.g. an inflated
        customers_planned that included drop-ins under the old
        formula).
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

    def load_session_by_id(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Load a session by ``session_id`` directly.

        Distinct from :meth:`load_session` because the (route, date) key
        collapses to the most-recent session when more than one exists
        on the same pair (e.g. a backfilled session and a live one). The
        LLM-save fallback path needs the exact session the supervisor is
        viewing, not whichever happened to start last.
        """
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
                route = dict(zip(cols, row))
                rcode = route.get("route_code", "")
                date = route.get("sup_date_str", "")
                custs = self._fetch_all(cursor, self._s.customer_summary_table, session_id)
                items = self._fetch_all(cursor, self._s.item_details_table, session_id)
                return self._reconstruct(session_id, rcode, date, route, custs, items)
        except Exception as exc:
            logger.error("DB load_session_by_id failed for %s: %s", session_id, exc)
            return None

    def load_session(self, route_code: str, date: str) -> Optional[Dict[str, Any]]:
        if not self.available:
            return None
        try:
            with self._open_conn() as conn:
                cursor = conn.cursor()

                # Route header: pick the most-recent row for the (route,
                # date). Carries route-level state (LLM route analysis,
                # session_status, completion timestamps) -- always taken
                # from the latest writer so a fresh analysis overwrites
                # an older one. Customer + item rows go through the
                # canonical-aggregation path below so legacy data
                # scattered across multiple session_ids surfaces as one
                # unified set.
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
                route = dict(zip(cols, row))
                # Canonical sid for the rebuilt in-memory session. The
                # route_summary row may carry a legacy ``{route}_{date}_
                # {ts}_{uuid}`` sid from before the deterministic format
                # landed -- if we kept it, the auto-reconciler would
                # cache the Session under the legacy sid and every
                # downstream upsert (visit rows, item rows, route
                # header) would write to the legacy row alongside any
                # new deterministic-sid writes from /session/visit.
                # Normalising here means one Session, one sid, one
                # canonical row per (route, date) -- legacy rows
                # continue to surface via the aggregation read below.
                session_id = f"{route_code}_{date}"

                custs, items = self._fetch_canonical_rows(
                    cursor, route_code, date, visited_only=False,
                )
                return self._reconstruct(session_id, route_code, date, route, custs, items)
        except Exception as exc:
            logger.error("DB load failed for %s/%s: %s", route_code, date, exc)
            return None

    # ------------------------------------------------------------------
    # Per-LLM-payload saves
    #
    # Each LLM artifact (pre-visit briefing, post-visit customer review,
    # route review) lands via its own dedicated path so the per-visit
    # upsert and the LLM saves never race for the same column. All three
    # follow the same shape: ensure the parent row(s) exist via the same
    # idempotent upsert helpers, then UPDATE the single LLM column.
    # ------------------------------------------------------------------

    def _list_customer_codes_with(
        self, route_code: str, date: str, where_clause: str,
    ) -> set[str]:
        """Return distinct customer_codes matching ``where_clause`` across
        every session_id for ``(route_code, supervision_date)``.

        The three idempotency queries (briefing-done, performance-done,
        visited) share this exact shape: same join to route_summary by
        (route, date), same DISTINCT customer_code projection, only the
        per-row predicate changes. Aggregating across session_ids
        ensures the reconciler does not re-fire an LLM call (or re-write
        a visit row) for a customer whose previous artifact landed under
        a legacy ``{route}_{date}_{ts}_{uuid}`` sid before the
        deterministic format was rolled out -- without aggregation, the
        canonical-sid Session would see ``set()`` and burn LLM calls
        rebuilding state that already exists.

        ``where_clause`` is a static SQL fragment (no parameters); only
        ``route_code`` and ``date`` are bound. Callers pass trusted
        literals from this module."""
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

    def list_customers_with_performance_analysis(
        self, route_code: str, date: str,
    ) -> set[str]:
        """Return customer_codes that already have a non-NULL
        ``llm_performance_analysis`` anywhere in the (route, date)'s
        session rows. Mirror of :meth:`list_customers_with_briefing` --
        used by the auto-reconciler to skip customers whose post-visit
        analysis already ran."""
        return self._list_customer_codes_with(
            route_code, date, "cs.llm_performance_analysis IS NOT NULL",
        )

    def list_customers_with_briefing(self, route_code: str, date: str) -> set[str]:
        """Return customer_codes that already have a non-NULL
        ``llm_pre_visit_briefing`` anywhere in the (route, date)'s
        session rows.

        Used by the auto-reconciler so the in-flow briefing trigger is
        idempotent across ticks: customers whose briefing was generated
        on a previous tick are skipped, the LLM only fires for new
        planned customers."""
        return self._list_customer_codes_with(
            route_code, date, "cs.llm_pre_visit_briefing IS NOT NULL",
        )

    def list_visited_customer_codes(self, route_code: str, date: str) -> set[str]:
        """Return the set of customer_codes that have a **visit** row
        for the (route, date) -- ``visit_sequence > 0`` across any
        session_id. Pre-visit-briefing rows (``visit_sequence = 0``)
        are excluded so the reconciler's data phase still re-processes
        a briefed-but-not-yet-visited customer when YaumiLive surfaces
        their invoice.

        Used by the auto-visit reconciler's data phase to short-circuit
        customers whose visit has already been persisted, keeping each
        tick O(new visits) rather than O(all customers). The earlier
        version of this method returned every persisted code -- a
        briefing-only row would silently mask the customer's first
        live invoice. The ``visit_sequence > 0`` filter aligns the
        method with its docstring and its single caller's intent.
        """
        return self._list_customer_codes_with(
            route_code, date, "cs.visit_sequence > 0",
        )

    def save_pre_visit_briefing(
        self, snapshot: Dict[str, Any], customer_code: str, content: str,
    ) -> Dict[str, Any]:
        """Persist the LLM pre-visit briefing for one (session, customer)."""
        return self._save_customer_llm_column(
            snapshot, customer_code, "llm_pre_visit_briefing", content,
        )

    def save_customer_analysis(
        self, snapshot: Dict[str, Any], customer_code: str, content: str,
    ) -> Dict[str, Any]:
        """Persist the LLM post-visit customer review for one (session, customer)."""
        return self._save_customer_llm_column(
            snapshot, customer_code, "llm_performance_analysis", content,
        )

    def save_route_analysis(
        self, snapshot: Dict[str, Any], content: str,
    ) -> Dict[str, Any]:
        """Persist the LLM route-level review for one session."""
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
                    cursor.execute(
                        f"UPDATE {self._s.route_summary_table} "
                        f"SET [llm_route_analysis] = ? WHERE [session_id] = ?",
                        (str(content or ""), _str_clip(sid, _LEN_SESSION_ID)),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return {"success": True, "session_id": sid, "kind": "route_analysis"}
        except Exception as exc:
            logger.error("save_route_analysis failed (sid=%s): %s", sid, exc)
            return {"success": False, "error": str(exc)}

    def _save_customer_llm_column(
        self, snapshot: Dict[str, Any], customer_code: str,
        column: str, content: str,
    ) -> Dict[str, Any]:
        """Shared body for the customer-scoped LLM saves.

        Ensures the route header + the customer row exist via the same
        idempotent upsert helpers the visit path uses, then UPDATEs the
        targeted LLM column. ``column`` is a writer-controlled literal
        from a fixed set, never a request input -- safe to interpolate
        into the SQL even though the rest of the value is parameterised.

        **Idempotency guard.** The UPDATE includes a
        ``[column] IS NULL`` predicate so two writers (the live browser
        path and the auto-visit reconciler) racing on the same column
        cannot clobber each other. The first writer wins; the second
        sees rowcount = 0 and returns. A retry on the same content is
        a no-op.
        """
        if not self.available:
            return {"success": False, "error": "DB not configured"}
        sid = snapshot.get("sessionId", "")
        customer = snapshot.get("customers", {}).get(customer_code)
        if not sid or customer is None:
            return {
                "success": False,
                "error": f"snapshot missing sessionId or customer {customer_code!r}",
            }
        now = datetime.now()
        sup_date = snapshot.get("date") or now.strftime("%Y-%m-%d")
        clipped_sid  = _str_clip(sid, _LEN_SESSION_ID)
        clipped_cust = _str_clip(customer_code, _LEN_CUSTOMER_CODE)
        try:
            with self._open_conn() as conn:
                cursor = conn.cursor()
                try:
                    self._upsert_route_header(cursor, sid, snapshot, sup_date, now)
                    self._upsert_customer(cursor, sid, customer_code, customer, now)
                    # Only stamp the LLM column when we have real content.
                    # An LLM failure (None / empty) must still leave the
                    # row inserted so the route header's customers_planned
                    # count matches the customer-table cardinality -- the
                    # column simply stays NULL until a later retry lands.
                    # The IS NULL predicate is the race guard: a parallel
                    # writer that already wrote the column wins; this one
                    # silently no-ops.
                    if content not in (None, ""):
                        cursor.execute(
                            f"UPDATE {self._s.customer_summary_table} "
                            f"SET [{column}] = ? "
                            f"WHERE [session_id] = ? "
                            f"  AND [customer_code] = ? "
                            f"  AND [{column}] IS NULL",
                            (str(content), clipped_sid, clipped_cust),
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            return {
                "success": True,
                "session_id": sid,
                "customer_code": customer_code,
                "column": column,
            }
        except Exception as exc:
            logger.error(
                "LLM column save failed (sid=%s cust=%s col=%s): %s",
                sid, customer_code, column, exc,
            )
            return {"success": False, "error": str(exc)}

    def load_session_visits(self, route_code: str, date: str) -> Optional[Dict[str, Any]]:
        """Return saved per-customer visit data for a (route, date).

        Used by the live UI to hydrate already-visited customers on
        mount: the supervisor sees their actuals + score immediately,
        without re-running the briefing -> mark-visited flow.

        Shape mirrors what the ``/session/visit`` endpoint emits, so
        the frontend consumes either source through one code path:

        ``{
            "available": True,
            "session_id": <str>,
            "visits": {
                <customer_code>: {
                    "score":            {"score","coverage","accuracy"},
                    "actualSales":      {<item_code>: <qty>, ...},
                    "totalActual":      <int>,
                    "totalRecommended": <int>,
                },
                ...
            }
        }``
        """
        if not self.available:
            return None
        try:
            with self._open_conn() as conn:
                cursor = conn.cursor()
                # Route header: latest writer wins -- carries the most
                # recent route-level LLM analysis. Customer + item rows
                # are aggregated across every session_id for this
                # (route, date) so legacy data still surfaces as one
                # unified hydration payload. See ``_fetch_canonical_rows``
                # for the dedup rule.
                #
                # /session/saved hydrates the PLANNED tile grid only --
                # unplanned drop-ins go via /session/unplanned against
                # YaumiLive. The ``visited_only=True`` filter keeps the
                # "Visited X/Y" tile comparing like with like (X =
                # planned visited, Y = planned customers):
                #   visit_sequence > 0   -- rep invoiced this customer
                #   qty_recommended > 0  -- planned (drop-in rows sum
                #                           to 0 and are excluded).
                # ``qty_actual > 0`` is NOT the right signal: a planned
                # customer can be visited but buy only non-planned items
                # (qty_actual = 0, alsoBought populated) and must still
                # surface here.
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
                route_header = dict(zip(rh_cols, rh_row))
                # Canonical sid: see ``load_session`` for the rationale.
                # Frontend does not actually key off the saved-visits
                # session_id (it uses the one returned by /initialize),
                # but emitting the canonical form keeps the wire one
                # consistent identifier per (route, date).
                sid = f"{route_code}_{date}"

                custs, items = self._fetch_canonical_rows(
                    cursor, route_code, date, visited_only=True,
                )
                # Pull the full canonical set (visited + not) so the
                # redistribution shaper can see every planned downstream
                # candidate. The returned ``visits`` map still surfaces
                # ONLY visited rows (preserves the existing wire
                # contract); the unvisited rows are used purely to
                # populate the synthetic Session that ``shape_redistri
                # bution_view`` walks during replay.
                all_custs, all_items = self._fetch_canonical_rows(
                    cursor, route_code, date, visited_only=False,
                )
                return self._build_visits_payload(
                    sid, route_header, custs, items,
                    all_custs=all_custs, all_items=all_items,
                )
        except Exception as exc:
            logger.error(
                "DB load_session_visits failed for %s/%s: %s",
                route_code, date, exc,
            )
            return None

    @staticmethod
    def _build_visits_payload(
        sid: str, route_header: Dict, custs: List[Dict], items: List[Dict],
        *,
        all_custs: Optional[List[Dict]] = None,
        all_items: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        """Translate stored rows into the visit-result shape the live
        UI consumes. Single source of truth for the column-to-JSON
        mapping so the live ``/visit`` response and this hydration
        path stay aligned. LLM payloads ride along as raw strings (the
        analytics layer handles JSON parsing on the way back in).

        ``all_custs`` / ``all_items`` -- the full canonical set
        (visited + unvisited planned). Optional; when present the
        redistribution view is replayed for every visited row against
        the full downstream pool. When omitted (callers that haven't
        opted in yet) the visit rows carry the safe-default empty
        ``redistributions`` view.
        """
        actuals_by_cust: Dict[str, Dict[str, int]] = {}
        for it in items:
            cc = str(it.get("customer_code", ""))
            if not cc:
                continue
            ic = str(it.get("item_code", ""))
            if not ic:
                continue
            actuals_by_cust.setdefault(cc, {})[ic] = int(it.get("actual_qty") or 0)

        # Build the redistribution views in one pass. Local imports
        # avoid a hard import cycle (api.schemas -> models, but
        # core.redistribution imports api.schemas) and keep the
        # db_saver import surface stable for callers that only need
        # the per-visit upsert path.
        redistributions_by_cust: Dict[str, Dict[str, Any]] = {}
        try:
            from sales_supervision.core.redistribution import (
                compute_redistributions_for_saved_visits,
            )
            from sales_supervision.models.schemas import (
                ScoreResult,
                Session as _Session,
                SessionCustomer as _SessionCustomer,
                SessionItem as _SessionItem,
            )

            # Rebuild a minimal in-memory Session from the full canonical
            # row set so the shaper can see every planned downstream
            # customer. Items carry the original recommendation +
            # actuals; tier (UNPLANNED vs planned) is preserved so the
            # planned/unplanned classifier inside the shaper agrees with
            # the live path.
            roster_custs = all_custs if all_custs is not None else custs
            roster_items = all_items if all_items is not None else items
            items_by_cust_for_replay: Dict[str, List[_SessionItem]] = {}
            for it in roster_items:
                cc = str(it.get("customer_code", ""))
                if not cc:
                    continue
                ic = str(it.get("item_code", ""))
                if not ic:
                    continue
                # ``van_inventory_qty`` must round-trip into the replayed
                # SessionItem -- the ledger uses it to compute spare
                # van-load and drive allocation decisions on replay.
                # Without it, replayed allocations diverge from live.
                items_by_cust_for_replay.setdefault(cc, []).append(_SessionItem(
                    item_code=ic,
                    item_name=str(it.get("item_name") or ""),
                    recommended_qty=int(it.get("original_recommended_qty") or 0),
                    actual_qty=int(it.get("actual_qty") or 0),
                    was_sold=bool(it.get("was_item_sold") or False),
                    tier=str(it.get("recommendation_tier") or ""),
                    van_inventory_qty=int(it.get("van_inventory_qty") or 0),
                ))
            roster: Dict[str, _SessionCustomer] = {}
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
                        score=float(c.get("customer_performance_score") or 0),
                        coverage=float(c.get("sku_coverage_rate") or 0),
                        accuracy=float(c.get("customer_accuracy_avg") or 0),
                    ),
                )
            replay_session = _Session(
                session_id=sid,
                route_code=str(route_header.get("route_code") or ""),
                date=str(route_header.get("supervision_date") or ""),
                customers=roster,
            )

            # Walk visited customers in canonical visit_sequence order
            # (legacy rows with seq==0 land at the front, stable across
            # customer_code). Drop-ins (UNPLANNED-only items) are NOT
            # surfaced here -- the saved-visits payload only carries
            # planned visits; drop-ins ride on /session/unplanned.
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
        except Exception as exc:
            # Safe default: leave the per-visit redistributions empty.
            # ``logger.exception`` captures the traceback so future
            # regressions in the replay path are debuggable from the
            # supervision log without re-running the failing payload.
            logger.exception(
                "saved-visits redistribution replay failed (sid=%s): %s",
                sid, exc,
            )

        visits: Dict[str, Dict[str, Any]] = {}
        for c in custs:
            cc = str(c.get("customer_code", ""))
            if not cc:
                continue
            visits[cc] = {
                "score": {
                    "score":    float(c.get("customer_performance_score") or 0),
                    "coverage": float(c.get("sku_coverage_rate") or 0),
                    "accuracy": float(c.get("customer_accuracy_avg") or 0),
                },
                "actualSales":      actuals_by_cust.get(cc, {}),
                "totalActual":      int(c.get("qty_actual") or 0),
                "totalRecommended": int(c.get("qty_recommended") or 0),
                "preVisitBriefing":  c.get("llm_pre_visit_briefing") or None,
                "customerAnalysis":  c.get("llm_performance_analysis") or None,
                # Replayed redistribution view, or an empty default
                # when the replay couldn't run (legacy data, missing
                # full roster, etc.). Always built from the Pydantic
                # model so a wire-schema field added tomorrow stays in
                # sync without touching this fallback.
                "redistributions":   redistributions_by_cust.get(
                    cc, _empty_redistribution_dict(),
                ),
            }

        # Pre-aggregated visit totals so the live UI can show the
        # "Visited / Avg score" tiles on mount without re-summing the
        # ``visits`` map. Same shape ``Session.summary().visit_totals``
        # emits, so the live ``/visit`` response and this hydration
        # path drop into the same client-side state slot.
        visit_count = len(visits)
        if visit_count > 0:
            scores = [float(v["score"]["score"]) for v in visits.values()]
            avg_score: Optional[float] = round(sum(scores) / len(scores), 2)
        else:
            avg_score = None
        visit_totals = {
            "visited_count":     visit_count,
            "total_actual":      sum(int(v["totalActual"]) for v in visits.values()),
            "total_recommended": sum(int(v["totalRecommended"]) for v in visits.values()),
            "avg_score":         avg_score,
        }

        return {
            "available": True,
            "session_id": sid,
            "visits": visits,
            "routeAnalysis": route_header.get("llm_route_analysis") or None,
            "visit_totals": visit_totals,
        }

    def check_exists(self, route_code: str, date: str) -> bool:
        if not self.available:
            return False
        try:
            with self._open_conn() as conn:
                cursor = conn.cursor()
                # ``SELECT TOP 1 1`` short-circuits on the first match
                # via the (route_code, supervision_date) index --
                # ``COUNT(*)`` would scan every matching row.
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
        self, cursor: Any, sid: str, snapshot: Dict[str, Any],
        sup_date: str, now: datetime,
    ) -> None:
        """Atomic MERGE upsert for the route header row.

        Three quantity totals, distinct semantics, distinct columns:
          * planned_qty_recommended  = engine recommendation summed across
            ALL journey-plan customers (visited + not). Stable through
            the day; each visit re-asserts the same value.
          * visited_qty_recommended  = subset that flowed to customers
            actually visited so far -- denominator of fulfillment rate.
          * visited_qty_actual       = units the visited customers really
            bought. Cannot exceed visited_qty_recommended in practice.

        ``customer_completion_rate`` and ``qty_fulfillment_rate`` are
        PERSISTED computed columns -- the writer never touches them.
        ``session_started_at`` is bound here for the INSERT branch; the
        MERGE's UPDATE set excludes it so the stamp survives across
        every later upsert. ``llm_route_analysis`` is bound NULL for
        INSERT and excluded from UPDATE so ``save_route_analysis`` owns
        that column exclusively.
        """
        table = self._s.route_summary_table
        clipped_sid = _str_clip(sid, _LEN_SESSION_ID)
        rcode  = _str_clip(snapshot.get("routeCode"), _LEN_ROUTE_CODE)
        # Planned-only counts -- drop-ins (tier="UNPLANNED") are tracked
        # separately on the customer rows and surfaced to the UI via the
        # /session/unplanned endpoint, not via these route-header
        # counters. Falling back to the legacy totalCustomers /
        # visitedCustomers keys keeps backward compat for any caller
        # that hand-crafts a snapshot without going through summary().
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
                None,                      # llm_route_analysis  -- INSERT only
            ),
        )

    def _upsert_customer(
        self, cursor: Any, sid: str, customer_code: str,
        customer: Dict[str, Any], now: datetime,
    ) -> None:
        """Atomic MERGE upsert for one customer row in this session.

        ``qty_fulfillment_rate`` is a PERSISTED computed column.
        ``customer_accuracy_avg`` is the mean of per-item accuracies
        (live ``ScoreResult.accuracy``); stored alongside the computed
        qty ratio so reload returns both lenses without a second
        round-trip.

        ``llm_pre_visit_briefing`` and ``llm_performance_analysis`` are
        bound NULL for the INSERT branch and excluded from the MERGE's
        UPDATE set, so the dedicated save paths
        (``save_pre_visit_briefing`` / ``save_customer_analysis``) own
        those columns exclusively -- a re-tap of a visit can't wipe a
        previously-saved LLM payload.
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
                None,              # llm_pre_visit_briefing  -- INSERT only
                None,              # llm_performance_analysis -- INSERT only
                now,               # record_saved_at
            ),
        )

    def _replace_customer_items(
        self, cursor: Any, sid: str, customer_code: str,
        customer: Dict[str, Any], now: datetime,
    ) -> int:
        """DELETE this customer's prior item rows + INSERT the current set.

        Scoped tightly to (session_id, customer_code) so a visit only
        churns its own items -- other customers' rows on the same
        session_id are untouched.

        ``recommendation_adjustment`` is a PERSISTED computed column,
        so the writer never includes it in the INSERT tuple.
        """
        table = self._s.item_details_table
        clipped_sid  = _str_clip(sid, _LEN_SESSION_ID)
        clipped_cust = _str_clip(customer_code, _LEN_CUSTOMER_CODE)

        cursor.execute(_ITEMS_DELETE_TPL.format(table=table), (clipped_sid, clipped_cust))

        rows = []
        for it in customer.get("items", []):
            rec_qty = _to_int(it.get("RecommendedQuantity"))
            # Prefer the stored ``EffectiveRecommended`` so the DB row
            # matches the JSON snapshot exactly; fall back to
            # ``rec_qty + adjustment`` when the payload omits the field
            # OR sends an explicit ``null`` (which ``dict.get`` would
            # otherwise treat as a real value).
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
                # priority_score is DECIMAL(10,2); _clamp_score's
                # 999_999.99 cap fits inside that range comfortably.
                _clamp_score(it.get("PriorityScore")),
                _to_int(it.get("VanLoad")),
                _to_int(it.get("DaysSinceLastPurchase")),
                _clamp_score(it.get("PurchaseCycleDays")),
                # purchase_frequency_pct is DECIMAL(8,2) post-migration.
                _clamp_score(it.get("FrequencyPercent")),
                now,
            ))

        if rows:
            # Chunk so a customer with an unusually long item list never
            # ships a single multi-megabyte batch. Matches the pattern
            # demand_forecasting and recommended_order use for their
            # bulk pushes (settings-driven, no hard-coded chunk size).
            chunk = max(int(getattr(self._s.db, "executemany_chunk_size", 1000)), 1)
            tpl = _ITEM_INSERT_TPL.format(table=table)
            for i in range(0, len(rows), chunk):
                cursor.executemany(tpl, rows[i:i + chunk])
        return len(rows)

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def _fetch_all(self, cursor: Any, table: str, sid: str) -> List[Dict]:
        """Read every row for a session_id as a list of column dicts.
        ``SELECT *`` keeps the read schema-driven so PERSISTED computed
        columns (qty_fulfillment_rate, etc.) flow back automatically.

        ``ORDER BY id`` makes hydration deterministic so the UI sees
        items in stable insertion order across reloads -- avoids the
        list-jitter you'd otherwise get from SQL Server's free-form
        scan ordering."""
        cursor.execute(
            f"SELECT * FROM {table} WHERE session_id = ? ORDER BY id",
            (sid,),
        )
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    def _fetch_canonical_rows(
        self,
        cursor: Any,
        route_code: str,
        date: str,
        *,
        visited_only: bool,
    ) -> tuple[List[Dict], List[Dict]]:
        """Aggregate customer + item rows across every ``session_id`` for
        a ``(route_code, supervision_date)`` pair, deduped per customer.

        Earlier writers stamped a fresh ``{route}_{date}_{ts}_{uuid}``
        session_id on every ``/session/initialize`` call; the route and
        customer tables ended up with one parallel row per page-open and
        a naive ``TOP 1 ORDER BY session_started_at DESC`` read could
        only see one of them. Today every writer uses the deterministic
        ``{route}_{date}`` sid, but legacy rows still live in the tables
        -- this aggregation reads through both layouts safely.

        Dedup rule: per ``customer_code``, keep the row with the most
        recent ``record_saved_at`` (then ``id`` as tiebreaker). Items
        track the same ``(session_id, customer_code)`` pair the picked
        customer row carries, so adjustments and actuals stay paired
        with the customer write that produced them -- no item ever
        binds to a customer it wasn't written alongside.

        ``visited_only=True`` restricts to PLANNED VISITED rows
        (``visit_sequence > 0 AND qty_recommended > 0``) for the
        live-UI hydration tile. ``visited_only=False`` returns every
        row (including planned-but-not-yet-visited briefing rows) for
        the auto-reconciler's session rebuild on cold start.
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
            {k: v for k, v in zip(cols, row) if k != "rn"}
            for row in cursor.fetchall()
        ]
        if not custs:
            return [], []

        # Items: pull only the rows paired with the picked customer
        # writes -- match on (session_id, customer_code) so an old
        # session's item rows never bleed into the latest customer
        # row's display.
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
        items = [dict(zip(cols, row)) for row in cursor.fetchall()]
        return custs, items

    # ------------------------------------------------------------------
    # Reconstruct session from DB rows
    # ------------------------------------------------------------------

    @staticmethod
    def _reconstruct(sid: str, route_code: str, date: str, route: Dict, custs: List[Dict], items: List[Dict]) -> Dict:
        """Rebuild the session JSON from DB rows. Every JSON key returned
        here MUST mean the same thing it does on the live (pre-save)
        side, so round-trip diffing yields identical structures.

        Specifically:
          - item ``RecommendedQuantity`` = engine's ORIGINAL recommendation
            (matches live ``SessionItem.recommended_quantity``). Adjusted
            value is exposed under ``EffectiveRecommended``.
          - customer ``accuracy``       = live ``ScoreResult.accuracy``
            (mean of per-item accuracies); the qty-ratio fulfillment rate
            is exposed under ``qtyFulfillmentRate`` -- a distinct key.
          - route summary returns BOTH the planned-customer total and the
            visited-only total under explicit keys, so a reload never
            silently changes meaning under the same name.
        """
        # ---- items grouped by customer ----
        # Keys mirror ``SessionItem.to_dict()`` so a hydrated session
        # round-trips into the same shape a freshly-built one emits.
        # Explainability fields (WhyItem / WhyQuantity / Confidence /
        # PurchaseCount / AvgQuantityPerVisit / Signals / Source) are not
        # persisted in the supervision tables -- they live on the
        # recommended_order side. Hydrated rows therefore lack them; the
        # modal degrades to "-" for those sections (vs the live path
        # which carries them through ``SessionItem.raw``).
        items_by_cust: Dict[str, list] = {}
        for it in items:
            cc = str(it.get("customer_code", ""))
            items_by_cust.setdefault(cc, []).append({
                "ItemCode": it.get("item_code", ""),
                "ItemName": it.get("item_name", ""),
                # ``RecommendedQuantity`` is the engine's ORIGINAL value
                # so the JSON key keeps the same meaning across save and
                # reload. The supervisor-adjusted value is exposed
                # alongside as ``EffectiveRecommended`` (matches live
                # ``SessionItem.effective_recommended``).
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
                # Persisted display name (was empty before the customer_name
                # column existed; now round-trips faithfully).
                "customerName":     c.get("customer_name", "") or "",
                # ``visited`` derives from visit_sequence: positive means
                # the rep actually invoiced this customer (planned visit
                # OR unplanned drop-in). Pre-visit-briefing rows land
                # with seq = 0 and must NOT be reconstructed as visited
                # -- that would inflate session.visited_customers and
                # corrupt the next route-header upsert.
                "visited":          seq > 0,
                "visitSequence":    c.get("visit_sequence", 0),
                "score":            c.get("customer_performance_score", 0),
                "coverage":         c.get("sku_coverage_rate", 0),
                # ``accuracy`` keeps live semantics: mean of per-item
                # accuracies. ``qtyFulfillmentRate`` is the qty-ratio.
                "accuracy":             c.get("customer_accuracy_avg", 0),
                "qtyFulfillmentRate":   c.get("qty_fulfillment_rate", 0),
                "totalRecommended":     c.get("qty_recommended", 0),
                "totalActual":          c.get("qty_actual", 0),
                "totalItems":           c.get("skus_recommended", 0),
                "itemsSold":            c.get("skus_sold", 0),
                "items":                items_by_cust.get(cc, []),
                "llmAnalysis":          c.get("llm_performance_analysis", "") or "",
            }

        # ---- session header ----
        # Both planned (all-customer) and visited-only totals are
        # surfaced under distinct keys so a frontend that asks for either
        # gets the right number, every time.
        return {
            "sessionId":             sid,
            "routeCode":             route_code,
            "date":                  date,
            "status":                route.get("session_status", "closed"),
            "totalCustomers":        route.get("customers_planned", 0),
            "visitedCustomers":      route.get("customers_visited", 0),
            # Live ``Session.total_recommended`` (across all planned).
            "totalRecommended":      route.get("planned_qty_recommended", 0),
            # Live ``Session.total_actual`` is visited-only -- and so is
            # the visited_qty_actual we persist. Same value either way.
            "totalActual":           route.get("visited_qty_actual", 0),
            # Visited-only sub-totals exposed explicitly so the frontend
            # can tell at a glance which lens the number is in.
            "visitedRecommended":    route.get("visited_qty_recommended", 0),
            "visitedActual":         route.get("visited_qty_actual", 0),
            "visitedAchievement":    route.get("route_performance_score", 0),
            "customers":             customers,
        }
