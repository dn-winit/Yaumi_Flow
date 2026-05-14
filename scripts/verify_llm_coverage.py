"""Audit LLM-column coverage on supervision tables for a given date.

Reports per-route counts of:
  * planned customers (rows with qty_recommended > 0)
  * visited customers (visit_sequence > 0)
  * briefings populated (llm_pre_visit_briefing NOT NULL)
  * reviews populated  (llm_performance_analysis NOT NULL on visited rows)
  * route-level analysis (llm_route_analysis NOT NULL on route header)

Exits non-zero if any expected column is NULL -- usable as a CI / cron
canary. Pair with ``backfill_llm_columns.py --date <D>`` to close gaps.

Usage:
    python scripts/verify_llm_coverage.py --date 2026-05-14
    python scripts/verify_llm_coverage.py --date today
"""
from __future__ import annotations

import argparse
import sys
from datetime import date as _date_cls
from pathlib import Path

import pyodbc

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv()
from sales_supervision.config.settings import get_settings as ss_settings  # noqa: E402


def conn_str() -> str:
    db = ss_settings().db
    return (
        f"DRIVER={{ODBC Driver 17 for SQL Server}};"
        f"SERVER={db.host},{db.port};DATABASE={db.database};"
        f"UID={db.username};PWD={db.password};"
        "TrustServerCertificate=Yes;Connection Timeout=15"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD or 'today'")
    args = ap.parse_args()
    date = _date_cls.today().strftime("%Y-%m-%d") if args.date == "today" else args.date

    con = pyodbc.connect(conn_str())
    cur = con.cursor()
    cur.execute(
        """
        SELECT rs.route_code,
               COUNT(*) AS plan_rows,
               SUM(CASE WHEN cs.visit_sequence > 0 THEN 1 ELSE 0 END) AS vis,
               SUM(CASE WHEN cs.llm_pre_visit_briefing IS NOT NULL THEN 1 ELSE 0 END) AS brief,
               SUM(CASE WHEN cs.visit_sequence > 0 AND cs.llm_performance_analysis IS NOT NULL THEN 1 ELSE 0 END) AS review,
               MAX(CASE WHEN rs.llm_route_analysis IS NOT NULL THEN 1 ELSE 0 END) AS route_llm
        FROM yf_supervision_customers cs
        JOIN yf_supervision_routes rs ON rs.session_id = cs.session_id
        WHERE rs.supervision_date = ?
        GROUP BY rs.route_code
        ORDER BY rs.route_code
        """,
        (date,),
    )
    rows = cur.fetchall()
    con.close()

    if not rows:
        print(f"No supervision data found for {date}")
        return 2

    print(f"\n=== LLM coverage for {date} ===\n")
    print(f"{'route':>6s} {'plan':>5s} {'vis':>5s} {'brief':>10s} {'review':>10s} {'route_llm':>10s}")
    total_plan = total_vis = total_brief = total_review = total_rllm_ok = 0
    gaps = []
    for r in rows:
        route, plan, vis, brief, review, rllm = r
        plan, vis, brief, review, rllm = int(plan), int(vis), int(brief), int(review), int(rllm)
        total_plan += plan; total_vis += vis; total_brief += brief; total_review += review
        total_rllm_ok += rllm
        brief_str = f"{brief:>4d}/{plan:<4d}"
        review_str = f"{review:>4d}/{vis:<4d}"
        rllm_str = "Y" if rllm else "MISSING"
        print(f"{route:>6s} {plan:>5d} {vis:>5d} {brief_str:>10s} {review_str:>10s} {rllm_str:>10s}")
        if brief < plan:
            gaps.append((route, "briefings", plan - brief))
        if review < vis:
            gaps.append((route, "reviews", vis - review))
        if not rllm:
            gaps.append((route, "route_llm", 1))

    print()
    print(f"TOTAL  {total_plan:>5d} {total_vis:>5d} "
          f"{total_brief:>4d}/{total_plan:<4d}  {total_review:>4d}/{total_vis:<4d}  "
          f"{total_rllm_ok}/{len(rows)} route LLM(s)")
    if gaps:
        print(f"\n{len(gaps)} gap(s):")
        for route, kind, n in gaps:
            print(f"  - route {route}: {n} {kind} missing")
        print(f"\nTo fix: python scripts/backfill_llm_columns.py --date {date}")
        return 1
    print("\nAll LLM columns populated. OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
