"""Targeted backfill for missing LLM columns on a specific date.

Walks ``yf_supervision_*`` for the target date, identifies rows with
NULL ``llm_pre_visit_briefing`` / ``llm_performance_analysis`` /
``llm_route_analysis``, and fires the running llm_analytics + supervision
services to fill them.

Why HTTP (not in-process analyzer):
    The running llm_analytics service holds the canonical token bucket
    and cache. Going through HTTP shares both, so this script can't
    double-spend the rate budget or duplicate cache entries against the
    live cron. The cron and this script converge on the same DB state
    via the ``WHERE column IS NULL`` write guard.

Usage:
    python scripts/backfill_llm_columns.py --date 2026-05-14
    python scripts/backfill_llm_columns.py --date 2026-05-14 --skip-briefings
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import pyodbc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from sales_supervision.config.settings import get_settings as ss_settings  # noqa: E402


SS = ss_settings()
LLM_BASE = "http://localhost:8003/api/v1/analytics"
SS_BASE = "http://localhost:8004/api/v1/supervision"


def conn_str() -> str:
    db = SS.db
    return (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={db.host},{db.port};DATABASE={db.database};"
        f"UID={db.username};PWD={db.password};"
        "TrustServerCertificate=Yes;Connection Timeout=15"
    )


def fetch_targets(date: str, *, force: bool) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """Return three lists: briefing targets, review targets, route targets.

    With ``force=False`` we only pick rows whose LLM column is NULL.
    With ``force=True`` we pick every eligible row regardless of whether
    a column is already populated -- used to wipe and regenerate after
    a code/prompt change so stale analyses can't outlive the bug they
    captured. The wipe itself happens in ``wipe_llm_columns`` so the
    targets and the wipe stay in lock-step.
    """
    con = pyodbc.connect(conn_str())
    cur = con.cursor()

    null_brief = "AND cs.llm_pre_visit_briefing IS NULL" if not force else ""
    null_review = "AND cs.llm_performance_analysis IS NULL" if not force else ""
    null_route = "AND llm_route_analysis IS NULL" if not force else ""

    # Briefing targets -- every planned customer row.
    cur.execute(
        f"""
        SELECT rs.session_id, rs.route_code, cs.customer_code, cs.customer_name
        FROM yf_supervision_customers cs
        JOIN yf_supervision_routes rs ON rs.session_id = cs.session_id
        WHERE rs.supervision_date = ?
          AND cs.qty_recommended > 0
          {null_brief}
        ORDER BY rs.route_code, cs.customer_code
        """,
        (date,),
    )
    briefing_targets = [
        {"session_id": r[0], "route_code": r[1], "customer_code": r[2],
         "customer_name": r[3] or ""}
        for r in cur.fetchall()
    ]

    # Review targets -- every visited customer.
    cur.execute(
        f"""
        SELECT rs.session_id, rs.route_code, cs.customer_code,
               cs.customer_performance_score, cs.sku_coverage_rate, cs.customer_accuracy_avg
        FROM yf_supervision_customers cs
        JOIN yf_supervision_routes rs ON rs.session_id = cs.session_id
        WHERE rs.supervision_date = ?
          AND cs.visit_sequence > 0
          {null_review}
        ORDER BY rs.route_code, cs.customer_code
        """,
        (date,),
    )
    review_targets = [
        {"session_id": r[0], "route_code": r[1], "customer_code": r[2],
         "performance_score": float(r[3] or 0),
         "coverage": float(r[4] or 0),
         "accuracy": float(r[5] or 0)}
        for r in cur.fetchall()
    ]

    # Route targets -- every route header.
    cur.execute(
        f"""
        SELECT session_id, route_code
        FROM yf_supervision_routes
        WHERE supervision_date = ?
          {null_route}
        ORDER BY route_code
        """,
        (date,),
    )
    route_targets = [{"session_id": r[0], "route_code": r[1]} for r in cur.fetchall()]

    con.close()
    return briefing_targets, review_targets, route_targets


def wipe_llm_columns(date: str) -> Tuple[int, int]:
    """NULL every ``llm_*`` column on ``date``'s supervision rows.

    Required before a forced regeneration so the saver's
    ``WHERE column IS NULL`` race guard lets the new payload land.
    Returns ``(customer_rows_wiped, route_rows_wiped)`` for the log.
    """
    con = pyodbc.connect(conn_str())
    cur = con.cursor()
    cur.execute(
        """
        UPDATE cs
           SET llm_pre_visit_briefing = NULL,
               llm_performance_analysis = NULL
          FROM yf_supervision_customers cs
          JOIN yf_supervision_routes rs ON rs.session_id = cs.session_id
         WHERE rs.supervision_date = ?
        """,
        (date,),
    )
    cust_wiped = cur.rowcount
    cur.execute(
        "UPDATE yf_supervision_routes SET llm_route_analysis = NULL "
        "WHERE supervision_date = ?",
        (date,),
    )
    route_wiped = cur.rowcount
    con.commit()
    con.close()
    return cust_wiped, route_wiped


def fetch_items_for(session_id: str, customer_code: str) -> List[Dict[str, Any]]:
    con = pyodbc.connect(conn_str())
    cur = con.cursor()
    cur.execute(
        """
        SELECT item_code, item_name,
               adjusted_recommended_qty, actual_qty,
               recommendation_tier, priority_score,
               days_since_last_purchase, purchase_cycle_days, purchase_frequency_pct,
               was_item_sold
        FROM yf_supervision_items
        WHERE session_id = ? AND customer_code = ?
        """,
        (session_id, customer_code),
    )
    out: List[Dict[str, Any]] = []
    for r in cur.fetchall():
        out.append({
            "item_code": r[0] or "",
            "item_name": r[1] or "",
            "recommended_qty": int(r[2] or 0),
            "actual_qty": int(r[3] or 0),
            "tier": r[4] or "",
            "priority_score": float(r[5] or 0),
            "days_since_last_purchase": int(r[6] or 0),
            "purchase_cycle_days": float(r[7] or 0),
            "frequency_percent": float(r[8] or 0),
            "was_sold": bool(r[9]),
        })
    con.close()
    return out


def fetch_route_visits(session_id: str, route_code: str, date: str) -> Tuple[List[Dict], int]:
    """Visited customers for the route, in the shape analyze/route expects.
    Also returns total planned customer count for the same route+date."""
    con = pyodbc.connect(conn_str())
    cur = con.cursor()
    cur.execute(
        """
        SELECT cs.customer_code, cs.customer_name,
               cs.customer_performance_score, cs.sku_coverage_rate,
               cs.customer_accuracy_avg, cs.qty_actual, cs.qty_recommended,
               cs.skus_recommended, cs.skus_sold
        FROM yf_supervision_customers cs
        JOIN yf_supervision_routes rs ON rs.session_id = cs.session_id
        WHERE rs.route_code = ? AND rs.supervision_date = ?
          AND cs.visit_sequence > 0
        """,
        (route_code, date),
    )
    visited = [
        {
            "customer_code": r[0],
            "customer_name": r[1] or "",
            "is_planned": True,
            "score": float(r[2] or 0),
            "coverage": float(r[3] or 0),
            "accuracy": float(r[4] or 0),
            "total_actual": int(r[5] or 0),
            "total_recommended": int(r[6] or 0),
            "item_count": int(r[7] or 0),
            "items_sold": int(r[8] or 0),
        }
        for r in cur.fetchall()
    ]
    cur.execute(
        """
        SELECT COUNT(*) FROM yf_supervision_customers cs
        JOIN yf_supervision_routes rs ON rs.session_id = cs.session_id
        WHERE rs.route_code = ? AND rs.supervision_date = ?
          AND cs.qty_recommended > 0
        """,
        (route_code, date),
    )
    total_planned = int(cur.fetchone()[0] or 0)
    con.close()
    return visited, total_planned


def fire_briefing(date: str, t: Dict) -> bool:
    items = fetch_items_for(t["session_id"], t["customer_code"])
    if not items:
        print(f"  brief {t['route_code']}/{t['customer_code']}: SKIP (no items)")
        return False
    body = {
        "customer_code": t["customer_code"],
        "customer_name": t["customer_name"],
        "route_code": t["route_code"],
        "date": date,
        "items": items,
    }
    with httpx.Client(timeout=120) as c:
        r = c.post(f"{LLM_BASE}/analyze/pre-visit", json=body)
    if r.status_code != 200:
        print(f"  brief {t['route_code']}/{t['customer_code']}: HTTP {r.status_code}")
        return False
    payload = r.json()
    if not payload.get("success"):
        reason = payload.get("data", {}).get("_degraded_reason", "?")
        print(f"  brief {t['route_code']}/{t['customer_code']}: DEGRADED ({reason})")
        return False
    # Persist via supervision
    save = {
        "session_id": t["session_id"],
        "customer_code": t["customer_code"],
        "content": json.dumps(payload["data"], default=str),
    }
    with httpx.Client(timeout=30) as c:
        sr = c.post(f"{SS_BASE}/session/briefing", json=save)
    ok = sr.status_code == 200 and sr.json().get("success")
    print(f"  brief {t['route_code']}/{t['customer_code']}: {'OK' if ok else 'SAVE FAIL'}")
    return bool(ok)


def fire_review(date: str, t: Dict) -> bool:
    items = fetch_items_for(t["session_id"], t["customer_code"])
    if not items:
        print(f"  review {t['route_code']}/{t['customer_code']}: SKIP (no items)")
        return False
    body = {
        "customer_code": t["customer_code"],
        "route_code": t["route_code"],
        "date": date,
        "customer_data": [],
        "current_items": items,
        "performance_score": t["performance_score"],
        "coverage": t["coverage"],
        "accuracy": t["accuracy"],
    }
    with httpx.Client(timeout=120) as c:
        r = c.post(f"{LLM_BASE}/analyze/customer", json=body)
    if r.status_code != 200:
        print(f"  review {t['route_code']}/{t['customer_code']}: HTTP {r.status_code}")
        return False
    payload = r.json()
    if not payload.get("success"):
        reason = payload.get("data", {}).get("_degraded_reason", "?")
        print(f"  review {t['route_code']}/{t['customer_code']}: DEGRADED ({reason})")
        return False
    save = {
        "session_id": t["session_id"],
        "customer_code": t["customer_code"],
        "content": json.dumps(payload["data"], default=str),
    }
    with httpx.Client(timeout=30) as c:
        sr = c.post(f"{SS_BASE}/session/customer-analysis", json=save)
    ok = sr.status_code == 200 and sr.json().get("success")
    print(f"  review {t['route_code']}/{t['customer_code']}: {'OK' if ok else 'SAVE FAIL'}")
    return bool(ok)


def fire_route(date: str, t: Dict) -> bool:
    visited, total_planned = fetch_route_visits(t["session_id"], t["route_code"], date)
    if not visited:
        print(f"  route {t['route_code']}: SKIP (no visited customers)")
        return False
    body = {
        "route_code": t["route_code"],
        "date": date,
        "visited_customers": visited,
        "total_customers": total_planned,
        "total_actual": sum(v["total_actual"] for v in visited),
        "total_recommended": sum(v["total_recommended"] for v in visited),
        "actual_customer_codes": [v["customer_code"] for v in visited],
    }
    with httpx.Client(timeout=120) as c:
        r = c.post(f"{LLM_BASE}/analyze/route", json=body)
    if r.status_code != 200:
        print(f"  route {t['route_code']}: HTTP {r.status_code}")
        return False
    payload = r.json()
    if not payload.get("success"):
        reason = payload.get("data", {}).get("_degraded_reason", "?")
        print(f"  route {t['route_code']}: DEGRADED ({reason})")
        return False
    save = {
        "session_id": t["session_id"],
        "content": json.dumps(payload["data"], default=str),
    }
    with httpx.Client(timeout=30) as c:
        sr = c.post(f"{SS_BASE}/session/route-analysis", json=save)
    ok = sr.status_code == 200 and sr.json().get("success")
    print(f"  route {t['route_code']}: {'OK' if ok else 'SAVE FAIL'}")
    return bool(ok)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--force", action="store_true",
                    help="Wipe today's LLM columns first and regenerate every row "
                         "-- use after a code or prompt change so stale analyses "
                         "can't outlive the bug they captured.")
    ap.add_argument("--skip-briefings", action="store_true")
    ap.add_argument("--skip-reviews", action="store_true")
    ap.add_argument("--skip-routes", action="store_true")
    args = ap.parse_args()

    if args.force:
        cust, route = wipe_llm_columns(args.date)
        print(f"Wiped LLM columns: {cust} customer rows, {route} route rows")

    print(f"Targets for {args.date} (force={args.force}):")
    briefings, reviews, routes = fetch_targets(args.date, force=args.force)
    print(f"  {len(briefings)} briefings, {len(reviews)} reviews, {len(routes)} routes")
    print()

    n_ok = 0
    n_fail = 0
    t0 = time.time()

    if not args.skip_routes and routes:
        print("=== Routes ===")
        for t in routes:
            if fire_route(args.date, t):
                n_ok += 1
            else:
                n_fail += 1
        print()

    if not args.skip_reviews and reviews:
        print("=== Customer reviews ===")
        for t in reviews:
            if fire_review(args.date, t):
                n_ok += 1
            else:
                n_fail += 1
        print()

    if not args.skip_briefings and briefings:
        print("=== Pre-visit briefings ===")
        for t in briefings:
            if fire_briefing(args.date, t):
                n_ok += 1
            else:
                n_fail += 1

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed:.0f}s -- ok={n_ok} fail={n_fail}")
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
