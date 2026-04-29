"""
Saves supervision sessions to YaumiAIML (3 tables).
Column order matches scripts/create_tables.sql exactly.
YaumiLive is never written to.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import pyodbc

from sales_supervision.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# Column orders matching create_tables.sql (excludes id -- auto-generated)
_ROUTE_COLS = [
    "session_id", "route_code", "supervision_date",
    "total_customers_planned", "total_customers_visited", "customer_completion_rate",
    "total_qty_recommended", "total_qty_actual", "qty_fulfillment_rate",
    "route_performance_score", "session_status", "session_started_at",
]

_CUSTOMER_COLS = [
    "session_id", "customer_code", "visit_sequence",
    "total_skus_recommended", "total_skus_sold", "sku_coverage_rate",
    "total_qty_recommended", "total_qty_actual", "qty_fulfillment_rate",
    "customer_performance_score", "llm_performance_analysis", "record_saved_at",
]

_ITEM_COLS = [
    "session_id", "customer_code", "item_code", "item_name",
    "original_recommended_qty", "adjusted_recommended_qty", "recommendation_adjustment",
    "actual_qty", "was_manually_edited", "was_item_sold",
    "recommendation_tier", "priority_score", "van_inventory_qty",
    "days_since_last_purchase", "purchase_cycle_days", "purchase_frequency_pct",
    "record_saved_at",
]


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


def _str_clip(value: Any, max_len: int) -> str:
    """Coerce to string and clip at the schema's NVARCHAR length so we
    never trip a "String or binary data would be truncated" error."""
    if value is None:
        return ""
    s = str(value)
    return s if len(s) <= max_len else s[:max_len]


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

    def _connect(self) -> pyodbc.Connection:
        return pyodbc.connect(self._db.connection_string(), autocommit=False)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save_session(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not self.available:
            return {"success": False, "error": "DB not configured (set SS_DB_HOST + table names)"}

        session_id = data.get("sessionId", "")
        customers = data.get("customers", {})
        now = datetime.now()

        conn: Optional[pyodbc.Connection] = None
        try:
            conn = self._connect()
            conn.timeout = self._db.query_timeout  # pyodbc query timeout lives on the connection
            cursor = conn.cursor()
            cursor.fast_executemany = True

            # All three writes share one transaction so DELETE+INSERT
            # stays atomic per session_id -- any failure rolls the whole
            # session back, never leaves partial rows behind.
            self._write_route(cursor, session_id, data, now)
            cust_count = self._write_customers(cursor, session_id, customers, now)
            item_count = self._write_items(cursor, session_id, customers, now)

            conn.commit()
            logger.info("Session %s -> DB: 1 route, %d customers, %d items", session_id, cust_count, item_count)
            return {"success": True, "session_id": session_id, "customers": cust_count, "items": item_count}
        except Exception as exc:
            logger.error("DB save failed for session %s: %s", session_id, exc)
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            return {"success": False, "error": str(exc)}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load_session(self, route_code: str, date: str) -> Optional[Dict[str, Any]]:
        if not self.available:
            return None
        conn: Optional[pyodbc.Connection] = None
        try:
            conn = self._connect()
            conn.timeout = self._db.query_timeout  # pyodbc query timeout lives on the connection
            cursor = conn.cursor()

            cursor.execute(
                f"SELECT TOP 1 session_id FROM {self._s.route_summary_table} "
                f"WHERE route_code = ? AND supervision_date = ? ORDER BY session_started_at DESC",
                (route_code, date),
            )
            row = cursor.fetchone()
            if not row:
                return None
            session_id = row[0]

            route = self._fetch_dict(cursor, self._s.route_summary_table, session_id)
            custs = self._fetch_all(cursor, self._s.customer_summary_table, session_id)
            items = self._fetch_all(cursor, self._s.item_details_table, session_id)
            return self._reconstruct(session_id, route_code, date, route, custs, items)
        except Exception as exc:
            logger.error("DB load failed for %s/%s: %s", route_code, date, exc)
            return None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def check_exists(self, route_code: str, date: str) -> bool:
        if not self.available:
            return False
        conn: Optional[pyodbc.Connection] = None
        try:
            conn = self._connect()
            conn.timeout = self._db.query_timeout  # pyodbc query timeout lives on the connection
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT COUNT(*) FROM {self._s.route_summary_table} "
                f"WHERE route_code = ? AND supervision_date = ?",
                (route_code, date),
            )
            return cursor.fetchone()[0] > 0
        except Exception as exc:
            logger.warning("DB exists-check failed for %s/%s: %s", route_code, date, exc)
            return False
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    def _write_route(self, cursor: Any, sid: str, data: Dict, now: datetime) -> None:
        table = self._s.route_summary_table
        cursor.execute(f"DELETE FROM {table} WHERE session_id = ?", (sid,))

        total_c = _to_int(data.get("totalCustomers"))
        visited_c = _to_int(data.get("visitedCustomers"))
        # Fulfillment rate is "of what we recommended for the customers we
        # actually visited, how much sold". Mixing all-customer recommended
        # with visited-only actuals would let unvisited customers drag the
        # rate down, which has nothing to do with forecast quality.
        visited_rec = _to_int(data.get("visitedRecommended"))
        visited_act = _to_int(data.get("visitedActual"))

        ph = ", ".join("?" for _ in _ROUTE_COLS)
        cursor.execute(
            f"INSERT INTO {table} ({', '.join(f'[{c}]' for c in _ROUTE_COLS)}) VALUES ({ph})",
            (
                _str_clip(sid, _LEN_SESSION_ID),
                _str_clip(data.get("routeCode"), _LEN_ROUTE_CODE),
                data.get("date", ""),
                total_c, visited_c,
                round(visited_c / max(total_c, 1) * 100, 1),
                visited_rec, visited_act,
                round(visited_act / max(visited_rec, 1) * 100, 1),
                _to_float(data.get("visitedAchievement")),
                _str_clip(data.get("status", "closed"), _LEN_STATUS),
                now,
            ),
        )

    def _write_customers(self, cursor: Any, sid: str, customers: Dict, now: datetime) -> int:
        table = self._s.customer_summary_table
        cursor.execute(f"DELETE FROM {table} WHERE session_id = ?", (sid,))

        clipped_sid = _str_clip(sid, _LEN_SESSION_ID)
        rows = []
        for code, c in customers.items():
            if not c.get("visited"):
                continue
            total_items = _to_int(c.get("totalItems", len(c.get("items", []))))
            sold = _to_int(c.get("itemsSold"))
            total_rec = _to_int(c.get("totalRecommended"))
            total_act = _to_int(c.get("totalActual"))
            rows.append((
                clipped_sid,
                _str_clip(code, _LEN_CUSTOMER_CODE),
                _to_int(c.get("visitSequence")),
                total_items, sold,
                _to_float(c.get("coverage")),
                total_rec, total_act,
                round(total_act / max(total_rec, 1) * 100, 1),
                _to_float(c.get("score")),
                # llm_performance_analysis is NVARCHAR(MAX) so no clip; coerce
                # to str so a non-string LLM payload (rare but possible)
                # doesn't crash pyodbc's parameter binder.
                str(c.get("llmAnalysis") or ""),
                now,
            ))

        if rows:
            ph = ", ".join("?" for _ in _CUSTOMER_COLS)
            cursor.executemany(
                f"INSERT INTO {table} ({', '.join(f'[{c}]' for c in _CUSTOMER_COLS)}) VALUES ({ph})",
                rows,
            )
        return len(rows)

    def _write_items(self, cursor: Any, sid: str, customers: Dict, now: datetime) -> int:
        table = self._s.item_details_table
        cursor.execute(f"DELETE FROM {table} WHERE session_id = ?", (sid,))

        clipped_sid = _str_clip(sid, _LEN_SESSION_ID)
        rows = []
        for code, c in customers.items():
            if not c.get("visited"):
                continue
            clipped_code = _str_clip(code, _LEN_CUSTOMER_CODE)
            for it in c.get("items", []):
                rec_qty = _to_int(it.get("recommendedQuantity"))
                adj = _to_int(it.get("adjustment"))
                # Prefer the stored effectiveRecommended so the DB row matches
                # the JSON snapshot exactly; fall back to the sum only if an
                # older payload omits it.
                effective = _to_int(it.get("effectiveRecommended", rec_qty + adj))
                rows.append((
                    clipped_sid,
                    clipped_code,
                    _str_clip(it.get("itemCode"), _LEN_ITEM_CODE),
                    _str_clip(it.get("itemName"), _LEN_ITEM_NAME),
                    rec_qty, effective, adj,
                    _to_int(it.get("actualQuantity")),
                    1 if it.get("wasEdited") else 0,
                    1 if it.get("wasSold") else 0,
                    _str_clip(it.get("tier"), _LEN_TIER),
                    _to_float(it.get("priorityScore")),
                    _to_int(it.get("vanInventoryQty")),
                    _to_int(it.get("daysSinceLastPurchase")),
                    _to_float(it.get("purchaseCycleDays")),
                    _to_float(it.get("frequencyPercent")),
                    now,
                ))

        if rows:
            ph = ", ".join("?" for _ in _ITEM_COLS)
            cursor.executemany(
                f"INSERT INTO {table} ({', '.join(f'[{c}]' for c in _ITEM_COLS)}) VALUES ({ph})",
                rows,
            )
        return len(rows)

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def _fetch_dict(self, cursor: Any, table: str, sid: str) -> Dict:
        cursor.execute(f"SELECT * FROM {table} WHERE session_id = ?", (sid,))
        cols = [d[0] for d in cursor.description]
        row = cursor.fetchone()
        return dict(zip(cols, row)) if row else {}

    def _fetch_all(self, cursor: Any, table: str, sid: str) -> List[Dict]:
        cursor.execute(f"SELECT * FROM {table} WHERE session_id = ?", (sid,))
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in cursor.fetchall()]

    # ------------------------------------------------------------------
    # Reconstruct session from DB rows
    # ------------------------------------------------------------------

    @staticmethod
    def _reconstruct(sid: str, route_code: str, date: str, route: Dict, custs: List[Dict], items: List[Dict]) -> Dict:
        # Group items by customer
        items_by_cust: Dict[str, list] = {}
        for it in items:
            cc = str(it.get("customer_code", ""))
            items_by_cust.setdefault(cc, []).append({
                "itemCode": it.get("item_code", ""),
                "itemName": it.get("item_name", ""),
                "recommendedQuantity": it.get("adjusted_recommended_qty", 0),
                "actualQuantity": it.get("actual_qty", 0),
                "adjustment": it.get("recommendation_adjustment", 0),
                "wasSold": bool(it.get("was_item_sold", 0)),
                "wasEdited": bool(it.get("was_manually_edited", 0)),
                "tier": it.get("recommendation_tier", ""),
                "priorityScore": it.get("priority_score", 0),
                "daysSinceLastPurchase": it.get("days_since_last_purchase", 0),
                "purchaseCycleDays": it.get("purchase_cycle_days", 0),
                "frequencyPercent": it.get("purchase_frequency_pct", 0),
                "vanInventoryQty": it.get("van_inventory_qty", 0),
            })

        customers = {}
        for c in custs:
            cc = str(c.get("customer_code", ""))
            customers[cc] = {
                "customerCode": cc, "customerName": "", "visited": True,
                "visitSequence": c.get("visit_sequence", 0),
                "score": c.get("customer_performance_score", 0),
                "coverage": c.get("sku_coverage_rate", 0),
                "accuracy": c.get("qty_fulfillment_rate", 0),
                "totalRecommended": c.get("total_qty_recommended", 0),
                "totalActual": c.get("total_qty_actual", 0),
                "totalItems": c.get("total_skus_recommended", 0),
                "itemsSold": c.get("total_skus_sold", 0),
                "items": items_by_cust.get(cc, []),
                "llmAnalysis": c.get("llm_performance_analysis", ""),
            }

        return {
            "sessionId": sid, "routeCode": route_code, "date": date,
            "status": route.get("session_status", "closed"),
            "totalCustomers": route.get("total_customers_planned", 0),
            "visitedCustomers": route.get("total_customers_visited", 0),
            "totalRecommended": route.get("total_qty_recommended", 0),
            "totalActual": route.get("total_qty_actual", 0),
            "customers": customers,
        }
