"""Populate yf_supervision_* for the last N working days using existing
yf_recommended_orders rows + customer_data.csv invoice replay. Fires the
in-flow LLM trio (pre-visit briefing, customer analysis, route summary)
per session.

Production-grade contract: every LLM column on every supervision row
must hold a real (non-fallback) payload, otherwise non-zero exit. The
verification step at the end is what enforces this.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from pathlib import Path

import pandas as pd
import pyodbc
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from data_import.config.settings import get_settings as di_settings
from recommended_order.config.settings import get_settings as ro_settings
from sales_supervision.config.settings import get_settings as ss_settings
from sales_supervision.core.session import SessionManager
from sales_supervision.models.schemas import (
    ScoreResult, SessionCustomer, SessionItem,
)
from sales_supervision.services.db_saver import DbSaver
from llm_analytics.config.settings import Settings as LLMSettings
from llm_analytics.core.analyzer import Analyzer

# --- knobs --------------------------------------------------------------
N_WORKING_DAYS = 10
BACKFILL_TAG = "backfill"
# LLM behaviour (rate limit, max_tokens, temperature, timeout, model)
# is sourced entirely from .env via ``LLMSettings()`` -- no script-level
# overrides. One change in .env reflects in both the live llm_analytics
# service and this backfill, so they can never drift.

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("regen-sup")


# Map yf_recommended_orders column -> the keys SessionManager.create_session
# expects on each recommendation dict.
DB_TO_REC = {
    "customer_code": "CustomerCode",
    "customer_name": "CustomerName",
    "item_code": "ItemCode",
    "item_name": "ItemName",
    "recommended_quantity": "RecommendedQuantity",
    "tier": "Tier",
    "priority_score": "PriorityScore",
    "days_since_last_purchase": "DaysSinceLastPurchase",
    "purchase_cycle_days": "PurchaseCycleDays",
    "frequency_percent": "FrequencyPercent",
    "van_load": "VanLoad",
}


def is_fallback(result):
    """Detect Analyzer fallback responses (rate-limit miss, provider error).
    Fallbacks have a ``reason`` key set to a known marker; we never persist
    those -- the column stays NULL so verification flags the gap and the
    next run retries cleanly."""
    if not isinstance(result, dict):
        return True
    reason = str(result.get("reason", "")).lower()
    if reason and any(t in reason for t in (
        "rate limit", "rate_limit", "fallback",
        "provider error", "unavailable", "error",
    )):
        return True
    if all(not v for k, v in result.items() if k != "reason"):
        return True
    return False


def to_json(payload):
    try:
        return json.dumps(payload or {}, default=str, ensure_ascii=False)
    except Exception:
        return "{}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--end-date",
        default=None,
        help="Latest TrxDate (YYYY-MM-DD) to include; window is the last N working days <= end-date",
    )
    parser.add_argument(
        "--start-date",
        default=None,
        help="Earliest TrxDate (YYYY-MM-DD) to include. When set, --days/N_WORKING_DAYS is ignored "
             "and the window is exactly [start-date, end-date].",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=N_WORKING_DAYS,
        help=f"Number of trailing working days to include (default {N_WORKING_DAYS}). Ignored if --start-date is set.",
    )
    parser.add_argument(
        "--no-clear",
        action="store_true",
        help="Skip the supervision-table wipe; only the days in this window are upserted.",
    )
    args = parser.parse_args()

    ro_s = ro_settings()
    ss_s = ss_settings()
    ro_conn_str = ro_s.db.aiml_connection_string
    ss_conn_str = ss_s.db.connection_string()

    # 1. Working days -- last N unique TrxDates with invoices. Path comes
    #    from the unified data_import settings so the script never drifts
    #    out of sync with the live service's import location.
    di_s = di_settings()
    customer_data_path = di_s.data_path(di_s.customer_data_file)
    log.info("Loading %s ...", customer_data_path)
    inv = pd.read_csv(
        customer_data_path,
        usecols=["TrxDate", "RouteCode", "CustomerCode", "ItemCode", "TotalQuantity"],
        low_memory=False,
    )
    inv["TrxDate"] = pd.to_datetime(inv["TrxDate"], errors="coerce").dt.strftime("%Y-%m-%d")
    inv["RouteCode"] = inv["RouteCode"].astype(str).str.strip()
    inv["CustomerCode"] = inv["CustomerCode"].astype(str).str.strip()
    inv["ItemCode"] = inv["ItemCode"].astype(str).str.strip()
    # Ceil so a 17.4-unit invoiced quantity reads as 18, never 17 --
    # matches the same quantity contract the engine and supervision
    # tables enforce ("can't sell 17.4 of a SKU").
    import numpy as _np
    inv["TotalQuantity"] = _np.ceil(
        pd.to_numeric(inv["TotalQuantity"], errors="coerce").fillna(0)
    ).astype(int)
    inv = inv.dropna(subset=["TrxDate"])
    all_days = sorted(inv["TrxDate"].unique().tolist())
    if args.end_date:
        all_days = [d for d in all_days if d <= args.end_date]
        if not all_days:
            log.error("no TrxDates <= %s in customer_data.csv", args.end_date)
            return 1
    if args.start_date:
        working_days = [d for d in all_days if d >= args.start_date]
        if not working_days:
            log.error("no TrxDates in [%s, %s] window", args.start_date, args.end_date or "max")
            return 1
    else:
        working_days = all_days[-args.days:]
    log.info(
        "Window: %s -> %s (%d working days)",
        working_days[0], working_days[-1], len(working_days),
    )

    routes = list(ro_s.route_codes)
    log.info(
        "Grid: %d days x %d routes = %d (day, route) pairs",
        len(working_days), len(routes), len(working_days) * len(routes),
    )
    inv_groups = inv.groupby(["TrxDate", "RouteCode"], sort=False)

    # 2. Verify recommendations exist for these days.
    ro_conn = pyodbc.connect(ro_conn_str, timeout=15)
    ro_cur = ro_conn.cursor()
    ro_cur.execute(
        f"SELECT trx_date, COUNT(*) FROM {ro_s.recommendation_table} "
        f"WHERE CAST(trx_date AS DATE) >= ? AND CAST(trx_date AS DATE) <= ? "
        f"GROUP BY trx_date ORDER BY trx_date",
        (working_days[0], working_days[-1]),
    )
    rec_per_day = {str(r[0]): r[1] for r in ro_cur.fetchall()}
    missing_recs = [d for d in working_days if d not in rec_per_day]
    if missing_recs:
        log.error("recommendations missing for: %s", missing_recs)
        return 1
    log.info(
        "recommendations cover all %d days; %s rows in window",
        len(working_days), f"{sum(rec_per_day.values()):,}",
    )

    # 3. Clear supervision tables (unless --no-clear; e.g. incremental top-up
    #    where we want to preserve days already loaded outside the window).
    ss_conn = pyodbc.connect(ss_conn_str, timeout=15)
    ss_cur = ss_conn.cursor()
    if args.no_clear:
        # Wipe only the rows that fall inside this window so an idempotent
        # re-run doesn't double-insert. FK CASCADE on session_id handles children.
        log.info("Incremental: deleting yf_supervision_routes for window %s..%s", working_days[0], working_days[-1])
        ss_cur.execute(
            f"DELETE FROM {ss_s.route_summary_table} "
            f"WHERE supervision_date >= ? AND supervision_date <= ?",
            (working_days[0], working_days[-1]),
        )
        log.info("  deleted %d in-window route rows (FK cascade clears children)", ss_cur.rowcount)
    else:
        log.info("Clearing yf_supervision_* ...")
        ss_cur.execute(f"DELETE FROM {ss_s.route_summary_table}")
        log.info(
            "  DELETE FROM %s -> %d rows (FK cascade clears children)",
            ss_s.route_summary_table, ss_cur.rowcount,
        )
    for tbl in [
        ss_s.route_summary_table,
        ss_s.customer_summary_table,
        ss_s.item_details_table,
    ]:
        if args.no_clear:
            continue
        ss_cur.execute(f"DBCC CHECKIDENT('{tbl}', RESEED, 0)")
        if ss_cur.description:
            ss_cur.fetchall()
    ss_conn.commit()
    ss_conn.close()

    # 4. Init pieces.
    mgr = SessionManager()
    db_saver = DbSaver(ss_s)
    if not db_saver.available:
        log.error("DbSaver not configured")
        return 1

    # Single source of truth: .env. Same values feed live llm_analytics
    # service and this backfill so behaviour can never drift between paths.
    llm_s = LLMSettings()
    analyzer = Analyzer(llm_s)
    health = analyzer.health()
    log.info(
        "Analyzer ready: %s/%s available=%s rate=%d/%ds max_tokens=%d",
        health.get("provider"), health.get("model"), health.get("available"),
        llm_s.rate_limit_max_requests, llm_s.rate_limit_window_seconds,
        llm_s.max_tokens,
    )
    if not health.get("available"):
        log.error("Analyzer unavailable -- check LLM_API_KEY in .env")
        return 1

    # 5. Per-session loader from DB.
    def load_recs(day, rc):
        ro_cur.execute(
            f"SELECT customer_code, customer_name, item_code, item_name, "
            f"       recommended_quantity, tier, priority_score, "
            f"       days_since_last_purchase, purchase_cycle_days, "
            f"       frequency_percent, van_load "
            f"FROM {ro_s.recommendation_table} "
            f"WHERE CAST(trx_date AS DATE) = ? AND route_code = ?",
            (day, rc),
        )
        cols = [d[0] for d in ro_cur.description]
        rows = []
        for r in ro_cur.fetchall():
            d = dict(zip(cols, r))
            rows.append({DB_TO_REC.get(k, k): v for k, v in d.items()})
        return rows

    # 6. Main loop.
    stats = {
        "sessions": 0, "customers": 0, "items": 0,
        "unplanned_visits": 0,
        "skipped_no_recs": 0, "skipped_no_visit": 0, "errors": 0,
        "llm_briefings": 0, "llm_customer_analyses": 0,
        "llm_route_summaries": 0, "llm_errors": 0,
    }
    t0 = time.time()
    for day in working_days:
        day_t0 = time.time()
        for rc in routes:
            recs = load_recs(day, rc)
            if not recs:
                stats["skipped_no_recs"] += 1
                continue
            session = mgr.create_session(rc, day, recs)
            session.session_id = (
                f"{rc}_{day}_{BACKFILL_TAG}_{uuid.uuid4().hex[:6]}"
            )

            if (day, rc) not in inv_groups.groups:
                stats["skipped_no_visit"] += 1
                continue
            slice_df = inv_groups.get_group((day, rc))
            invoiced_codes = set(slice_df["CustomerCode"].unique())
            planned_codes = set(session.customers.keys())
            visited_planned = invoiced_codes & planned_codes
            visited_unplanned = invoiced_codes - planned_codes
            visited_all = visited_planned | visited_unplanned
            if not visited_all:
                stats["skipped_no_visit"] += 1
                continue

            # Process planned visits the standard way: scoring runs against
            # the recommendation baseline.
            for cc in visited_planned:
                cust_inv = slice_df[slice_df["CustomerCode"] == cc]
                actual_sales = dict(
                    zip(cust_inv["ItemCode"], cust_inv["TotalQuantity"])
                )
                mgr.process_visit(session, cc, actual_sales)

            # Register unplanned drop-ins as synthetic SessionCustomers --
            # tier=UNPLANNED, recommended_qty=0, actual_qty=invoiced_qty.
            # They land in yf_supervision_customers/_items via the same
            # upsert path so end-of-day route summaries include them.
            for cc in visited_unplanned:
                cust_inv = slice_df[slice_df["CustomerCode"] == cc]
                items = [
                    SessionItem(
                        item_code=str(it_row["ItemCode"]),
                        item_name="",
                        recommended_qty=0,
                        actual_qty=int(it_row["TotalQuantity"]),
                        was_sold=int(it_row["TotalQuantity"]) > 0,
                        tier="UNPLANNED",
                    )
                    for _, it_row in cust_inv.iterrows()
                    if int(it_row["TotalQuantity"]) > 0
                ]
                if not items:
                    continue
                seq = session.visit_sequence_counter + 1
                session.customers[cc] = SessionCustomer(
                    customer_code=cc,
                    customer_name="",
                    items=items,
                    visited=True,
                    visit_sequence=seq,
                    score=ScoreResult(score=0.0, coverage=0.0, accuracy=0.0),
                )
                stats["unplanned_visits"] += 1

            snapshot = session.to_dict()
            for cc in visited_all:
                res = db_saver.upsert_visit(snapshot, cc)
                if not res.get("success"):
                    log.warning(
                        "    upsert failed %s/%s/%s: %s",
                        day, rc, cc, res.get("error"),
                    )
                    stats["errors"] += 1
                else:
                    stats["customers"] += 1
                    stats["items"] += int(res.get("items", 0))
            stats["sessions"] += 1

            # Step 1: pre-visit briefing for every PLANNED customer.
            # Drop-ins (tier=UNPLANNED, recommended_qty=0) have no plan
            # to brief on, so we skip them by design.
            for ccode, cust in session.customers.items():
                if not cust.items:
                    continue
                if all(it.tier == "UNPLANNED" for it in cust.items):
                    continue
                items_payload = [
                    {
                        "item_code": it.item_code,
                        "item_name": it.item_name,
                        "recommended_qty": int(it.recommended_qty),
                        "tier": it.tier,
                        "purchase_cycle_days": float(it.purchase_cycle_days or 0.0),
                        "days_since_last_purchase": int(it.days_since_last_purchase or 0),
                        "frequency_percent": float(it.frequency_percent or 0.0),
                    }
                    for it in cust.items
                ]
                try:
                    result = analyzer.pre_visit_briefing(
                        customer_code=ccode,
                        customer_name=cust.customer_name,
                        route_code=rc,
                        date=day,
                        items=items_payload,
                    )
                except Exception as exc:
                    log.warning(
                        "    briefing failed %s/%s/%s: %s",
                        day, rc, ccode, exc,
                    )
                    stats["llm_errors"] += 1
                    continue
                if is_fallback(result):
                    stats["llm_errors"] += 1
                    continue
                save = db_saver.save_pre_visit_briefing(snapshot, ccode, to_json(result))
                if save.get("success"):
                    stats["llm_briefings"] += 1
                else:
                    stats["llm_errors"] += 1

            # Step 2: customer analysis for every visited customer
            # (planned AND unplanned). Unplanned customers get the
            # analysis run against their actual purchases with zero
            # recommended baseline -- the LLM will note "out-of-plan
            # purchase".
            for ccode in visited_all:
                cust = session.customers.get(ccode)
                if cust is None:
                    continue
                items_payload = [
                    {
                        "item_code": it.item_code,
                        "item_name": it.item_name,
                        "recommended_qty": int(it.recommended_qty),
                        "actual_qty": int(it.actual_qty),
                        "tier": it.tier,
                    }
                    for it in cust.items
                ]
                try:
                    result = analyzer.analyze_customer(
                        customer_code=ccode,
                        route_code=rc,
                        date=day,
                        customer_data=pd.DataFrame(),
                        current_items=items_payload,
                        performance_score=cust.score.score,
                        coverage=cust.score.coverage,
                        accuracy=cust.score.accuracy,
                    )
                except Exception as exc:
                    log.warning(
                        "    analysis failed %s/%s/%s: %s",
                        day, rc, ccode, exc,
                    )
                    stats["llm_errors"] += 1
                    continue
                if is_fallback(result):
                    stats["llm_errors"] += 1
                    continue
                save = db_saver.save_customer_analysis(snapshot, ccode, to_json(result))
                if save.get("success"):
                    stats["llm_customer_analyses"] += 1
                else:
                    stats["llm_errors"] += 1

            # Step 3: ROUTE SUMMARY -- end-of-day retrospective.
            # Fires when at least ONE customer was visited (planned or
            # unplanned). Idempotent at DB level: ``save_route_analysis``
            # UPSERTs into ``llm_route_analysis`` so the column reflects
            # the most-recent state when the iteration ends. Includes
            # both planned and unplanned visits in the payload.
            visited_session_custs = [
                c for c in session.customers.values() if c.visited
            ]
            if visited_session_custs:
                visited_payload = [
                    {
                        "customer_code": c.customer_code,
                        "customer_name": c.customer_name,
                        "is_planned": not all(it.tier == "UNPLANNED" for it in c.items),
                        "score": c.score.score,
                        "coverage": c.score.coverage,
                        "accuracy": c.score.accuracy,
                        "total_actual": c.total_actual,
                        "total_recommended": c.total_recommended,
                    }
                    for c in visited_session_custs
                ]
                planned_count = sum(
                    1 for c in session.customers.values()
                    if any(it.tier != "UNPLANNED" for it in c.items)
                )
                try:
                    result = analyzer.analyze_route(
                        route_code=rc,
                        date=day,
                        visited_customers=visited_payload,
                        total_customers=planned_count,
                        total_actual=float(session.total_actual),
                        total_recommended=float(session.total_recommended),
                        pre_context=None,
                        actual_customer_codes=set(
                            c.customer_code for c in visited_session_custs
                        ),
                    )
                except Exception as exc:
                    log.warning("    route-llm failed %s/%s: %s", day, rc, exc)
                    stats["llm_errors"] += 1
                    continue
                if is_fallback(result):
                    stats["llm_errors"] += 1
                    continue
                save = db_saver.save_route_analysis(snapshot, to_json(result))
                if save.get("success"):
                    stats["llm_route_summaries"] += 1
                else:
                    stats["llm_errors"] += 1

        log.info(
            "  %s done: sessions=%d cust=%d brief=%d analyses=%d route=%d errors=%d (%.1fs)",
            day, stats["sessions"], stats["customers"], stats["llm_briefings"],
            stats["llm_customer_analyses"], stats["llm_route_summaries"],
            stats["llm_errors"], time.time() - day_t0,
        )

    ro_conn.close()
    log.info("Total: %s in %.1fs", stats, time.time() - t0)

    # 7. Verification -- hard stop if any LLM column missing.
    ss_conn = pyodbc.connect(ss_conn_str, timeout=15)
    ss_cur = ss_conn.cursor()
    ss_cur.execute(f"SELECT COUNT(*) FROM {ss_s.route_summary_table}")
    n_routes = ss_cur.fetchone()[0]
    ss_cur.execute(f"SELECT COUNT(*) FROM {ss_s.customer_summary_table}")
    n_custs = ss_cur.fetchone()[0]
    ss_cur.execute(f"SELECT COUNT(*) FROM {ss_s.item_details_table}")
    n_items = ss_cur.fetchone()[0]
    print(f"\n=== FINAL ROW COUNTS ===")
    print(f"  yf_supervision_routes      = {n_routes}")
    print(f"  yf_supervision_customers   = {n_custs}")
    print(f"  yf_supervision_items       = {n_items}")

    # Scoped coverage:
    #   * route_llm     -- required on every route (every route ran ≥1 visit)
    #   * briefing      -- required for PLANNED customers only (qty_recommended > 0).
    #                      Unplanned drop-ins have nothing to brief on.
    #   * performance   -- required for VISITED customers only (qty_actual > 0).
    #                      Planned-no-shows have no visit to analyze.
    ss_cur.execute(
        f"SELECT SUM(CASE WHEN llm_route_analysis IS NULL THEN 1 ELSE 0 END), "
        f"       COUNT(*) "
        f"FROM {ss_s.route_summary_table}"
    )
    missing_route, total_route = ss_cur.fetchone()
    ss_cur.execute(
        f"SELECT "
        f"  SUM(CASE WHEN qty_recommended > 0 AND llm_pre_visit_briefing IS NULL THEN 1 ELSE 0 END), "
        f"  SUM(CASE WHEN qty_recommended > 0 THEN 1 ELSE 0 END), "
        f"  SUM(CASE WHEN qty_actual > 0 AND llm_performance_analysis IS NULL THEN 1 ELSE 0 END), "
        f"  SUM(CASE WHEN qty_actual > 0 THEN 1 ELSE 0 END), "
        f"  SUM(CASE WHEN (qty_recommended IS NULL OR qty_recommended=0) AND qty_actual > 0 THEN 1 ELSE 0 END), "
        f"  SUM(CASE WHEN qty_recommended > 0 AND (qty_actual IS NULL OR qty_actual=0) THEN 1 ELSE 0 END), "
        f"  COUNT(*) "
        f"FROM {ss_s.customer_summary_table}"
    )
    miss_brief, planned_n, miss_perf, visited_n, unplanned_n, no_show_n, total_cust = ss_cur.fetchone()
    miss_brief = miss_brief or 0
    miss_perf = miss_perf or 0
    print(f"\n=== LLM COVERAGE ===")
    print(f"  llm_route_analysis        : {total_route - missing_route}/{total_route}  "
          f"({'OK' if missing_route == 0 else f'MISSING {missing_route}'})")
    print(f"  llm_pre_visit_briefing    : {planned_n - miss_brief}/{planned_n} planned  "
          f"({'OK' if miss_brief == 0 else f'MISSING {miss_brief}'})  "
          f"[+{unplanned_n} unplanned skipped by design]")
    print(f"  llm_performance_analysis  : {visited_n - miss_perf}/{visited_n} visited  "
          f"({'OK' if miss_perf == 0 else f'MISSING {miss_perf}'})  "
          f"[+{no_show_n} planned-no-show skipped by design]")
    ss_conn.close()

    print()
    if missing_route or miss_brief or miss_perf:
        print(
            f"[FAIL] missing LLM cells -- routes={missing_route} "
            f"briefings(planned)={miss_brief} analyses(visited)={miss_perf}"
        )
        return 2
    print("[PASS] every LLM column populated for its applicable scope.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
