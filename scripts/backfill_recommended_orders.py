"""One-shot backfill: clear ``yf_recommended_orders`` and regenerate
the last N working days end-to-end through the SAME helper the cron
uses (``recommended_order.api.routes._generate_routes``).

Why in-process and not HTTP
---------------------------
We import and call ``_generate_routes`` directly so the script runs
the byte-identical code path that the daily cron + ``/generate``
endpoint use -- same DataManager, same RecommendationEngine, same
DbPusher. Zero risk of a script-vs-service drift.

Why same helper
---------------
``_generate_routes`` is what wires reconciled van load (via
``DataManager.get_van_items`` -> ``enrich_with_load``) into the
recommendation engine. Calling it here means the backfill consumes the
same reconciled van load (= ``opening_stock + recommended_load``) the
production cron does. No special path, no parallel logic.

Run from project root:
    python scripts/backfill_recommended_orders.py [--days N]
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import pandas as pd
import pyodbc
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from data_import.config.settings import get_settings as di_settings  # noqa: E402
from recommended_order.api.routes import _generate_routes  # noqa: E402
from recommended_order.config.constants import RecommendationConstants  # noqa: E402
from recommended_order.config.settings import get_settings  # noqa: E402
from recommended_order.core.engine import RecommendationEngine  # noqa: E402
from recommended_order.data.manager import DataManager  # noqa: E402
from recommended_order.services.db_pusher import DbPusher  # noqa: E402
from recommended_order.services.storage.store import RecommendationStore  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ro-backfill")
# Quiet the per-route engine chatter; we surface day-level summaries.
logging.getLogger("recommended_order.core.engine").setLevel(logging.WARNING)
logging.getLogger("recommended_order.core.metrics").setLevel(logging.WARNING)


def working_days(n: int) -> list[str]:
    """Return the last ``n`` distinct dates that have invoiced sales in
    ``customer_data.csv`` -- the same definition the supervision-regen
    script uses, so backfills across services always agree on what a
    "working day" is.
    """
    di_s = di_settings()
    path = di_s.data_path(di_s.customer_data_file)
    if not path.exists():
        raise SystemExit(f"customer_data.csv not found at {path}; run data_import first")
    df = pd.read_csv(path, usecols=["TrxDate"], low_memory=False)
    df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce")
    days = sorted(df["TrxDate"].dt.strftime("%Y-%m-%d").dropna().unique().tolist())
    return days[-int(n):]


def wipe_recommendations(table: str, conn_str: str) -> int:
    """DELETE all rows from ``yf_recommended_orders``. Returns count.

    Uses autocommit + a single DELETE -- atomic at the engine level for
    a single statement. We don't TRUNCATE because the deployed user may
    not have ALTER permission and DELETE has equivalent effect for our
    row counts (~80k).
    """
    conn = pyodbc.connect(conn_str, autocommit=True, timeout=30)
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        before = cur.fetchone()[0]
        cur.execute(f"DELETE FROM {table}")
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        after = cur.fetchone()[0]
        if after != 0:
            raise RuntimeError(f"DELETE failed: {after} rows remain")
        return int(before)
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--days", type=int, default=30,
        help="Trailing working days to regenerate (default: 30)",
    )
    parser.add_argument(
        "--no-wipe", action="store_true",
        help="Skip the DELETE step (incremental top-up, not a clean reset)",
    )
    args = parser.parse_args()

    ro_s = get_settings()

    # 1. Wipe target table.
    if not args.no_wipe:
        log.info("Wiping %s ...", ro_s.recommendation_table)
        deleted = wipe_recommendations(
            ro_s.recommendation_table, ro_s.db.aiml_connection_string,
        )
        log.info("  deleted %d rows", deleted)

    # 2. Working-day window.
    days = working_days(args.days)
    log.info("Window: %s -> %s (%d working days)", days[0], days[-1], len(days))

    # 3. Initialise the same components ``/generate`` uses. DataManager
    #    seeds itself from the imports/ mirror data_import maintains.
    dm = DataManager(ro_s)
    init = dm.initialize()
    if not init.get("success"):
        log.error("DataManager init failed: %s", init.get("errors"))
        return 1
    engine = RecommendationEngine(RecommendationConstants())
    store = RecommendationStore(ro_s)
    pusher = DbPusher(ro_s)
    if not pusher.available:
        log.error("DbPusher not configured; aborting (DB writes would silently no-op).")
        return 1
    routes = list(ro_s.route_codes)
    log.info("Routes (%d): %s", len(routes), ",".join(routes))

    # 4. Generate per day. Uses the live ``_generate_routes`` helper so
    #    the path is identical to the cron + the /generate endpoint.
    overall_t0 = time.time()
    grand_total = 0
    per_day: list[dict] = []
    for i, day in enumerate(days, 1):
        # Refresh demand data per-day so reconciliation reads the day's
        # closing_stock state via ``enrich_with_load``.
        dm.ensure_fresh()
        t0 = time.time()
        res = _generate_routes(
            day, routes,
            dm=dm, engine=engine, store=store, pusher=pusher,
            skip_existing=False,  # full regeneration -- no caching of prior runs
        )
        dur = time.time() - t0
        records = int(res.get("total_records", 0))
        gen = int(res.get("routes_generated", 0))
        grand_total += records
        per_day.append({"day": day, "routes": gen, "records": records, "duration": dur})
        log.info("[%d/%d] %s -> %d routes / %d records in %.1fs",
                 i, len(days), day, gen, records, dur)

    # 5. Final tally.
    elapsed = time.time() - overall_t0
    log.info("=" * 60)
    log.info("DONE: %d days, %d records in %.1fs", len(days), grand_total, elapsed)
    log.info("=" * 60)
    for r in per_day:
        log.info("  %s  routes=%2d  records=%5d  %.1fs",
                 r["day"], r["routes"], r["records"], r["duration"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
