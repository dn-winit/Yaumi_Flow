"""
Read-only loader exposing supervision visits to the recommended_order
feedback loop.

Owns no business logic -- just the SQL needed to flatten the supervision
tables into the long item-level frame the feedback attribution code
expects. Lives in the supervision module because the schema is owned
here; recommended_order imports it to feed the closed-loop signal.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
import pyodbc

from sales_supervision.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class SessionDbLoader:
    """Pull visit-level outcomes from yf_supervision_* for the feedback loop.

    The query joins the customer header (visited customer set) with the
    item-detail rows (per-item actuals). One row out per (session, customer,
    item). Adversarial-filter / shrinkage-estimator code on the caller side
    handles the rest.
    """

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()

    @property
    def available(self) -> bool:
        return (
            self._s.db.configured
            and bool(self._s.route_summary_table)
            and bool(self._s.customer_summary_table)
            and bool(self._s.item_details_table)
        )

    def load_visits_in_window(self, *, start_date: str, end_date: str) -> pd.DataFrame:
        """Return one row per (route, date, session, customer, item) inside
        the window ``[start_date, end_date]`` inclusive.

        Schema:
          route_code, date, session_id, customer_code, item_code,
          actual_qty, was_sold, was_edited, visited
        """
        cols = [
            "route_code", "date", "session_id",
            "customer_code", "item_code",
            "actual_qty", "was_sold", "was_edited",
            "visited",
        ]
        if not self.available:
            return pd.DataFrame(columns=cols)

        # Per ``(route, date, customer)`` pick the most-recently written
        # customer row across all session_ids -- legacy data may carry
        # multiple parallel sids per (route, date) and a naive join would
        # multiply the feedback signal by the duplicate factor. The
        # ``picked`` CTE collapses to one canonical (session_id,
        # customer_code) per (route, date, customer); items inherit that
        # pairing so the long-format output has exactly one row per
        # (route, date, customer, item).
        sql = (
            f";WITH ranked AS ( "
            f"  SELECT r.route_code, r.supervision_date, "
            f"         c.session_id, c.customer_code, "
            f"         ROW_NUMBER() OVER ( "
            f"           PARTITION BY r.route_code, r.supervision_date, c.customer_code "
            f"           ORDER BY c.record_saved_at DESC, c.id DESC "
            f"         ) AS rn "
            f"  FROM {self._s.route_summary_table} r "
            f"  JOIN {self._s.customer_summary_table} c "
            f"    ON c.session_id = r.session_id "
            f"  WHERE r.supervision_date >= ? AND r.supervision_date <= ? "
            f"), "
            f"picked AS ( "
            f"  SELECT route_code, supervision_date, session_id, customer_code "
            f"  FROM ranked WHERE rn = 1 "
            f") "
            f"SELECT "
            f"  p.route_code, "
            f"  CONVERT(VARCHAR(10), p.supervision_date, 120) AS date, "
            f"  p.session_id, "
            f"  p.customer_code, "
            f"  i.item_code, "
            f"  i.actual_qty, "
            f"  i.was_item_sold AS was_sold, "
            f"  i.was_manually_edited AS was_edited "
            f"FROM picked p "
            f"JOIN {self._s.item_details_table} i "
            f"  ON i.session_id = p.session_id "
            f" AND i.customer_code = p.customer_code"
        )

        try:
            with pyodbc.connect(self._s.db.connection_string()) as conn:
                conn.timeout = self._s.db.query_timeout
                cursor = conn.cursor()
                cursor.execute(sql, (start_date, end_date))
                rows = cursor.fetchall()
                if not rows:
                    return pd.DataFrame(columns=cols)
                col_names = [d[0] for d in cursor.description]
                df = pd.DataFrame.from_records(
                    [tuple(r) for r in rows], columns=col_names,
                )
        except Exception as exc:
            logger.warning(
                "SessionDbLoader.load_visits_in_window failed (%s..%s): %s",
                start_date, end_date, exc,
            )
            return pd.DataFrame(columns=cols)

        if df.empty:
            return pd.DataFrame(columns=cols)

        # Normalise dtypes to match what the feedback attribution code expects.
        for k in ("route_code", "date", "session_id", "customer_code", "item_code"):
            df[k] = df[k].astype(str).str.strip()
        df["actual_qty"] = pd.to_numeric(df["actual_qty"], errors="coerce").fillna(0).astype(int)
        df["was_sold"] = df["was_sold"].astype(bool)
        df["was_edited"] = df["was_edited"].astype(bool)
        # Every row in this set is a visited customer (db_saver only writes
        # visited customers); the column is kept for shape compatibility
        # with the historical attribution pipeline.
        df["visited"] = True
        return df[
            [
                "route_code", "date", "session_id",
                "customer_code", "item_code",
                "actual_qty", "was_sold", "was_edited",
                "visited",
            ]
        ]
