"""Read-only flatten of yf_supervision_* into the long item-level frame recommended_order feedback expects."""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from common.db_pool import get_pool
from sales_supervision.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class SessionDbLoader:
    """One row per (session, customer, item) from yf_supervision_* for the feedback loop."""

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
        """One row per (route, date, session, customer, item) over [start_date, end_date] inclusive."""
        cols = [
            "route_code", "date", "session_id",
            "customer_code", "item_code",
            "actual_qty", "was_sold", "was_edited",
            "visited",
        ]
        if not self.available:
            return pd.DataFrame(columns=cols)

        # picked CTE collapses parallel legacy sids to one canonical (session_id, customer_code) per (route, date, customer).
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
            # Route through the shared connection pool -- raw pyodbc.connect
            # could deadlock against the SERIALIZABLE MERGE in db_saver under
            # concurrent cron+write load, and bypassed common.db_pool's
            # retry-with-deadlock-fast logic.
            pool = get_pool(
                self._s.db.connection_string(),
                query_timeout=self._s.db.query_timeout,
                autocommit=True,
            )
            with pool.acquire() as conn:
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

        # Normalise dtypes for the feedback attribution code.
        for k in ("route_code", "date", "session_id", "customer_code", "item_code"):
            df[k] = df[k].astype(str).str.strip()
        df["actual_qty"] = pd.to_numeric(df["actual_qty"], errors="coerce").fillna(0).astype(int)
        df["was_sold"] = df["was_sold"].astype(bool)
        df["was_edited"] = df["was_edited"].astype(bool)
        # db_saver only writes visited rows; column kept for shape compat.
        df["visited"] = True
        return df[
            [
                "route_code", "date", "session_id",
                "customer_code", "item_code",
                "actual_qty", "was_sold", "was_edited",
                "visited",
            ]
        ]
