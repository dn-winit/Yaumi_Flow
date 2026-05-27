"""Backfill ``yf_supervision_items`` so already-visited customers carry
their off-plan purchases on the wire.

Why: before today's fix, off-plan items (the ``alsoBought`` set) lived
only in the transient ``/visit`` response and never reached the items
table. After the fix, every NEW visit persists them -- but customers
already visited today carry a partial item set, so the green dot
correctly fires from YaumiLive yet the per-customer drawer renders
``actualSales`` for planned items only with no ``alsoBought`` strip.

How: for each route on the target date, walk planned customers that
YaumiLive shows as having off-plan purchases not yet in DB. Delete the
customer + items rows so the next ``/session/initialize`` reconcile
re-processes them through the new persistence path. Idempotent --
running twice is a no-op.

Usage:
    python scripts/backfill_off_plan_items.py --date 2026-05-14
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import httpx
import pyodbc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv()
from sales_supervision.config.settings import get_settings as ss_settings  # noqa: E402

SS = "http://localhost:8004/api/v1/supervision"
RO = "http://localhost:8001/api/v1/recommended-order"
DI = "http://localhost:8005/api/v1/data"

# Fleet sourced from the shared registry so this backfill, the daily
# cron, and the verification matrix all walk the same routes. Override
# via the ``YF_ROUTE_CODES`` env var without code edits.
from common.route_registry import get_route_codes as _get_route_codes  # noqa: E402

ROUTES = tuple(_get_route_codes())


def conn_str() -> str:
    db = ss_settings().db
    return (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={db.host},{db.port};DATABASE={db.database};"
        f"UID={db.username};PWD={db.password};"
        "TrustServerCertificate=Yes;Connection Timeout=15"
    )


def planned_items_per_customer(route: str, date: str) -> dict[str, set[str]]:
    with httpx.Client(timeout=120) as c:
        payload = c.post(f"{RO}/get", json={
            "route_code": route, "date": date, "limit": 5000, "offset": 0,
        }).json()
    out: dict[str, set[str]] = {}
    for r in payload.get("data") or []:
        cc = str(r.get("CustomerCode", ""))
        ic = str(r.get("ItemCode", ""))
        if cc and ic:
            out.setdefault(cc, set()).add(ic)
    return out


def live_actuals_per_customer(route: str, date: str) -> dict[str, dict[str, int]]:
    with httpx.Client(timeout=60) as c:
        payload = c.get(f"{DI}/eda/live-route-sales", params={
            "route_code": route, "date": date,
        }).json()
    out: dict[str, dict[str, int]] = {}
    for cust in payload.get("customers") or []:
        cc = str(cust.get("customer_code", ""))
        if not cc:
            continue
        out[cc] = {
            str(it["item_code"]): int(it.get("qty") or 0)
            for it in (cust.get("items") or [])
            if int(it.get("qty") or 0) > 0
        }
    return out


def persisted_item_codes(con: pyodbc.Connection, route: str, date: str) -> dict[str, set[str]]:
    cur = con.cursor()
    cur.execute(
        """
        SELECT cs.customer_code, i.item_code
        FROM yf_supervision_items i
        JOIN yf_supervision_customers cs ON cs.session_id=i.session_id AND cs.customer_code=i.customer_code
        JOIN yf_supervision_routes rs ON rs.session_id=cs.session_id
        WHERE rs.route_code=? AND rs.supervision_date=?
          AND cs.visit_sequence > 0
          AND cs.qty_recommended > 0
        """,
        (route, date),
    )
    out: dict[str, set[str]] = {}
    for cust, item in cur.fetchall():
        out.setdefault(str(cust), set()).add(str(item))
    return out


def delete_customer_rows(con: pyodbc.Connection, route: str, date: str, custs: list[str]) -> None:
    if not custs:
        return
    cur = con.cursor()
    qmarks = ",".join("?" * len(custs))
    cur.execute(
        f"""
        DELETE i FROM yf_supervision_items i
        JOIN yf_supervision_customers cs ON cs.session_id=i.session_id AND cs.customer_code=i.customer_code
        JOIN yf_supervision_routes rs ON rs.session_id=cs.session_id
        WHERE rs.route_code=? AND rs.supervision_date=? AND cs.customer_code IN ({qmarks})
        """,
        (route, date, *custs),
    )
    cur.execute(
        f"""
        DELETE cs FROM yf_supervision_customers cs
        JOIN yf_supervision_routes rs ON rs.session_id=cs.session_id
        WHERE rs.route_code=? AND rs.supervision_date=? AND cs.customer_code IN ({qmarks})
        """,
        (route, date, *custs),
    )
    con.commit()


def reinit_route(route: str, date: str) -> None:
    """Phase 1 of /session/initialize runs the reconciler synchronously.

    Raises ``RuntimeError`` on any non-2xx response so the caller can abort
    the batch instead of leaving the just-deleted rows orphaned.
    """
    with httpx.Client(timeout=120) as c:
        recs_resp = c.post(f"{RO}/get", json={
            "route_code": route, "date": date, "limit": 5000, "offset": 0,
        })
        recs_resp.raise_for_status()
        recs = recs_resp.json().get("data") or []
    with httpx.Client(timeout=240) as c:
        init_resp = c.post(f"{SS}/session/initialize", json={
            "route_code": route, "date": date, "recommendations": recs,
        })
        init_resp.raise_for_status()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would be re-processed without deleting / re-initialising.")
    args = ap.parse_args()

    con = pyodbc.connect(conn_str())
    total_targets = 0
    by_route: dict[str, list[tuple[str, int]]] = {}

    for route in ROUTES:
        planned = planned_items_per_customer(route, args.date)
        live = live_actuals_per_customer(route, args.date)
        persisted = persisted_item_codes(con, route, args.date)

        targets: list[tuple[str, int]] = []
        for cust, plan_items in planned.items():
            bought = live.get(cust) or {}
            if not bought:
                continue
            off_plan_bought = {ic for ic in bought if ic not in plan_items}
            if not off_plan_bought:
                continue
            persisted_for_cust = persisted.get(cust, set())
            missing = off_plan_bought - persisted_for_cust
            if missing:
                targets.append((cust, len(missing)))
        if targets:
            by_route[route] = targets
            total_targets += len(targets)
            print(f"Route {route}: {len(targets)} customers need backfill "
                  f"({sum(n for _, n in targets)} missing off-plan rows total)")

    print(f"\nTOTAL: {total_targets} customers across {len(by_route)} routes")

    if args.dry_run:
        print("\nDry-run; no changes made.")
        con.close()
        return 0

    if total_targets == 0:
        print("\nNothing to backfill.")
        con.close()
        return 0

    print("\nBackfilling...")
    failed_routes: list[str] = []
    for route, targets in by_route.items():
        custs = [c for c, _ in targets]
        delete_customer_rows(con, route, args.date, custs)
        # If the re-init fails for ANY reason (HTTP error, network, /get
        # returns garbage) we MUST stop -- those customers' rows are
        # already deleted; leaving them un-rehydrated would surface as
        # missing supervision entries in the UI. Caller decides whether
        # to retry the same route or restore from backup.
        try:
            reinit_route(route, args.date)
        except Exception as exc:
            con.rollback()
            failed_routes.append(route)
            print(
                f"Route {route}: re-init FAILED ({exc!r}); "
                f"{len(custs)} customer rows were already deleted -- "
                "aborting to prevent further orphaning."
            )
            break
        # Sanity: re-read so we can report a meaningful count.
        new_persisted = persisted_item_codes(con, route, args.date)
        backfilled = sum(1 for c in custs if c in new_persisted)
        print(
            f"Route {route}: re-initialised, {len(custs)} customers "
            f"re-processed ({backfilled} confirmed re-hydrated)."
        )

    con.close()
    if failed_routes:
        print(
            f"\nFAILED on routes {failed_routes}. Re-run --dry-run to see "
            "what's still missing and re-process those routes only."
        )
        return 1
    print("\nDone. Refresh the Visit page -- alsoBought should now hydrate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
