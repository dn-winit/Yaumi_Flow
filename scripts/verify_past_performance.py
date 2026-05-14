"""End-to-end consistency audit for the past-performance / van-load
API surface. Calls each handler in-process and diffs the response
against direct ``yf_sales_transactions`` queries.

Five sections:
  1. /reconciliation/past-performance      (AccuracyDrawer source)
  2. /page-views/van-load                  (today's per-day view)
  3. /predictions/forecast/route-summary   (route tile grid)
  4. supervision <-> recommendations cross-source

Run:  python scripts/verify_past_performance.py
Exit codes: 0 = all checks pass, 1 = at least one drift detected.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

import pandas as pd  # noqa: E402

from data_import.core.database import DatabaseClient  # noqa: E402
from demand_forecasting_pipeline.api.dependencies import (  # noqa: E402
    get_artifact_service,
    get_van_load_service,
)
from demand_forecasting_pipeline.api.routes.page_views import (  # noqa: E402
    van_load_page_view,
)
from demand_forecasting_pipeline.api.routes.predictions import (  # noqa: E402
    get_future_route_summary,
)
from demand_forecasting_pipeline.api.routes.reconciliation import (  # noqa: E402
    past_performance,
)


ROUTE = "9108"
START = "2026-05-06"
END = "2026-05-12"
TODAY = "2026-05-13"


def main() -> int:
    db = DatabaseClient()
    results: list[tuple[str, bool, object, object]] = []

    def assert_eq(name: str, got, expect, tol: float = 0.5) -> None:
        ok = abs(float(got) - float(expect)) <= tol
        results.append((name, ok, got, expect))
        tag = "PASS" if ok else "FAIL"
        print(f"  [{tag}] {name}: got={got!r} expect={expect!r}")

    def header(t: str) -> None:
        bar = "=" * 90
        print(f"\n{bar}\n {t}\n{bar}")

    # ------------------------------------------------------------------
    # 1) /reconciliation/past-performance  (AccuracyDrawer source)
    # ------------------------------------------------------------------
    header(f"1. /reconciliation/past-performance route={ROUTE} {START}..{END}")
    resp = past_performance(
        route_code=ROUTE, start_date=START, end_date=END,
        item_codes=[], category_codes=[],
        van=get_van_load_service(), artifact_svc=get_artifact_service(),
    )
    print(f"  available={resp.available} message={resp.message}")
    print(f"  totals keys: {sorted((resp.totals or {}).keys())}")
    print(f"  daily count: {len(resp.daily or [])}")
    print(f"  items count: {len(resp.items or [])}")
    t = resp.totals or {}
    if t:
        print(
            f"  totals: rep_van={t.get('rep_van_load_total')} "
            f"our_van={t.get('recommended_van_load_total')} "
            f"sold={t.get('actual_sold_total')}"
        )

    # past-performance applies TWO filters in sequence:
    #   1. CARRY-AWARE on items: (recommended_load + opening_stock) > 0
    #   2. ACTIVE-DAY on dates:  only dates the route had real activity
    #      (rows in alloc / sales / returns CSVs). Dormant days where the
    #      rep didn't ship are silently dropped.
    # The DB comparison must mirror BOTH to be apples-to-apples. Active
    # days are recoverable from yf_sales_transactions: a day with rep
    # activity has at least one row with yaumi_fresh_load > 0 OR yaumi_leftover
    # > 0 OR actual_sold > 0 across the route.
    active_dates_df = db.execute_query(
        "SELECT DISTINCT CAST(trx_date AS NVARCHAR(10)) AS d "
        "FROM [YaumiAIML].[dbo].[yf_sales_transactions] "
        f"WHERE route_code='{ROUTE}' AND trx_date BETWEEN '{START}' AND '{END}' "
        "  AND (ISNULL(yaumi_fresh_load,0) > 0 OR ISNULL(yaumi_leftover,0) > 0 "
        "       OR ISNULL(actual_sold,0) > 0);",
        db="aiml",
    )
    active_set = {str(d) for d in active_dates_df["d"].tolist()}
    api_dates = {pd.Timestamp(row["date"]).strftime("%Y-%m-%d") for row in (resp.daily or [])}
    print(f"  API active_days={sorted(api_dates)}")
    print(f"  DB  active_days={sorted(active_set)}")
    active_list = "','".join(sorted(api_dates))
    db_t = db.execute_query(
        "SELECT "
        "  SUM(ISNULL(yaumi_total_van_load,0)) AS rep_van, "
        "  SUM(ISNULL(total_van_load,0)) AS our_van, "
        "  SUM(ISNULL(actual_sold,0)) AS sold, "
        "  COUNT(*) AS rows_in_scope "
        "FROM [YaumiAIML].[dbo].[yf_sales_transactions] "
        f"WHERE route_code='{ROUTE}' AND trx_date IN ('{active_list}') "
        "  AND (ISNULL(fresh_load,0) + ISNULL(opening_stock,0)) > 0;",
        db="aiml",
    ).iloc[0]
    print(f"  DB (carry-aware + same active days): rep_van={float(db_t['rep_van']):.1f} "
          f"our_van={float(db_t['our_van']):.1f} sold={float(db_t['sold']):.1f} rows={int(db_t['rows_in_scope'])}")
    # The drawer's items[] is widened to the disjoint union of:
    #   * anchor items (Predicted > 0, sourced from fc_df / yf_sales_transactions)
    #   * non-anchor items (sourced from closing_stock + load_allocation + sales_recent)
    # The DB query above filters to anchor scope, so we must filter the
    # API items to recommended_van_load > 0 before comparing. The full
    # totals (including non-anchor activity) are reconciled separately
    # via the items-vs-totals identity checks further down.
    anchor_rep_van  = round(sum(
        (it.rep_van_load or 0.0)
        for it in (resp.items or [])
        if (it.recommended_van_load or 0.0) > 0.0
    ), 2)
    anchor_sold     = round(sum(
        (it.actual_sold or 0.0)
        for it in (resp.items or [])
        if (it.recommended_van_load or 0.0) > 0.0
    ), 2)
    if t:
        assert_eq("anchor rep_van matches DB (Predicted>0 subset)",
                  anchor_rep_van, float(db_t["rep_van"]), tol=5.0)
        assert_eq("our_van matches DB (active+carry-aware)",
                  t.get("recommended_van_load_total", 0), float(db_t["our_van"]), tol=5.0)
        assert_eq("anchor sold matches DB (Predicted>0 subset)",
                  anchor_sold, float(db_t["sold"]), tol=5.0)
    if resp.daily and t:
        sum_rep = sum(d.get("rep_van_load", 0) for d in resp.daily)
        sum_our = sum(d.get("recommended_van_load", 0) for d in resp.daily)
        sum_sold = sum(d.get("actual_sold", 0) for d in resp.daily)
        assert_eq("daily rep == total", sum_rep,
                  t.get("rep_van_load_total", 0))
        assert_eq("daily our == total", sum_our,
                  t.get("recommended_van_load_total", 0))
        assert_eq("daily sold == total", sum_sold,
                  t.get("actual_sold_total", 0))
    # Items-vs-totals consistency
    if resp.items and t:
        sum_items_rep = sum((it.rep_van_load or 0) for it in resp.items)
        sum_items_our = sum((it.recommended_van_load or 0) for it in resp.items)
        sum_items_sold = sum((it.actual_sold or 0) for it in resp.items)
        assert_eq("items rep == total", sum_items_rep,
                  t.get("rep_van_load_total", 0), tol=5.0)
        assert_eq("items our == total", sum_items_our,
                  t.get("recommended_van_load_total", 0), tol=5.0)
        assert_eq("items sold == total", sum_items_sold,
                  t.get("actual_sold_total", 0), tol=5.0)

    # ------------------------------------------------------------------
    # 3) /page-views/van-load (today)
    # ------------------------------------------------------------------
    header(f"2. /page-views/van-load route={ROUTE} date={TODAY}")
    vl = van_load_page_view(
        route_code=ROUTE, date=TODAY, top_n=5,
        svc=get_artifact_service(), van_svc=get_van_load_service(),
    )
    print(
        f"  available={vl.available} items={len(vl.items)} "
        f"summary.van_load_qty={vl.summary.van_load_qty}"
    )
    print(f"  carried={vl.summary.carried_qty} issued={vl.summary.issued_qty}")
    assert_eq(
        "carried + issued == van_load_qty",
        vl.summary.carried_qty + vl.summary.issued_qty,
        vl.summary.van_load_qty,
        tol=0.05,
    )
    items_yaumi = sum((it.yaumi_total_van_load or 0) for it in vl.items)
    items_dormant = sum(1 for it in vl.items if it.forecast_dormant)
    db_today = db.execute_query(
        "SELECT SUM(ISNULL(yaumi_total_van_load,0)) AS rep_van, "
        "SUM(ISNULL(total_van_load,0)) AS our_van, "
        "SUM(CASE WHEN forecast_dormant=1 THEN 1 ELSE 0 END) AS dormant "
        "FROM [YaumiAIML].[dbo].[yf_sales_transactions] "
        f"WHERE route_code='{ROUTE}' AND trx_date='{TODAY}';",
        db="aiml",
    ).iloc[0]
    print(
        f"  DB rep_van={float(db_today['rep_van']):.1f} "
        f"our_van={float(db_today['our_van']):.1f} "
        f"dormant_rows={int(db_today['dormant'])}"
    )
    print(f"  items[].yaumi sum={items_yaumi:.1f} (van_load_view filters carry-aware)")
    print(f"  items[].forecast_dormant=True count={items_dormant}")

    # ------------------------------------------------------------------
    # 4) /predictions/forecast/route-summary
    # ------------------------------------------------------------------
    header(f"3. /predictions/forecast/route-summary date={TODAY}")
    rs = get_future_route_summary(date=TODAY, svc=get_artifact_service())
    print(f"  success={rs.success} routes={len(rs.routes) if rs.routes else 0} reconciled={rs.reconciled}")
    if rs.routes:
        for r in rs.routes:
            db_r = db.execute_query(
                "SELECT SUM(ISNULL(total_van_load,0)) AS v "
                "FROM [YaumiAIML].[dbo].[yf_sales_transactions] "
                f"WHERE route_code='{r.route_code}' AND trx_date='{TODAY}';",
                db="aiml",
            ).iloc[0]
            delta = abs(float(r.predicted_qty) - float(db_r["v"]))
            flag = " DRIFT" if delta > 5 else ""
            print(
                f"  {r.route_code}: tile={r.predicted_qty:>8.1f} "
                f"DB={float(db_r['v']):>8.1f} delta={delta:>6.2f}{flag}"
            )
            results.append((f"route-summary {r.route_code}", delta <= 5,
                            r.predicted_qty, float(db_r["v"])))

    # ------------------------------------------------------------------
    # 5) Supervision <-> recommendations cross-source
    # ------------------------------------------------------------------
    header(f"4. Supervision vs Recommendations cross-source date={TODAY}")
    xc = db.execute_query(
        "SELECT 'rec.cust' AS m, "
        "CAST(COUNT(DISTINCT customer_code) AS BIGINT) AS v "
        "FROM [YaumiAIML].[dbo].[yf_recommended_orders] "
        f"WHERE trx_date='{TODAY}' "
        "UNION ALL SELECT 'rec.qty', "
        "CAST(ISNULL(SUM(recommended_quantity),0) AS BIGINT) "
        "FROM [YaumiAIML].[dbo].[yf_recommended_orders] "
        f"WHERE trx_date='{TODAY}' "
        "UNION ALL SELECT 'sup.cust', "
        "CAST(COUNT(DISTINCT c.customer_code) AS BIGINT) "
        "FROM [YaumiAIML].[dbo].[yf_supervision_customers] c "
        "JOIN [YaumiAIML].[dbo].[yf_supervision_routes] r "
        "ON r.session_id=c.session_id "
        f"WHERE r.supervision_date='{TODAY}' "
        "UNION ALL SELECT 'sup.qty_rec', "
        "CAST(ISNULL(SUM(c.qty_recommended),0) AS BIGINT) "
        "FROM [YaumiAIML].[dbo].[yf_supervision_customers] c "
        "JOIN [YaumiAIML].[dbo].[yf_supervision_routes] r "
        "ON r.session_id=c.session_id "
        f"WHERE r.supervision_date='{TODAY}' "
        "UNION ALL SELECT 'sup.qty_act', "
        "CAST(ISNULL(SUM(c.qty_actual),0) AS BIGINT) "
        "FROM [YaumiAIML].[dbo].[yf_supervision_customers] c "
        "JOIN [YaumiAIML].[dbo].[yf_supervision_routes] r "
        "ON r.session_id=c.session_id "
        f"WHERE r.supervision_date='{TODAY}';",
        db="aiml",
    )
    print(xc.to_string(index=False))

    # ------------------------------------------------------------------
    # 7) BREAKDOWN POPOVERS -- summary tiles must equal sum of items[]
    # ------------------------------------------------------------------
    header("5. Popover consistency: tile total == sum(items field)")
    if vl.available and vl.summary and vl.items:
        # Per-item carry/fresh decomposition was pruned from the wire
        # (no surface renders it); the totals-level decomposition stays
        # on VanLoadSummaryView. The single identity that remains for
        # this section is: headline van_load_qty == sum(items[*].recommended_van_load).
        sum_van = sum((it.recommended_van_load or 0) for it in vl.items)
        assert_eq("Recommended van load: tile == sum(items.recommended_van_load)",
                  vl.summary.van_load_qty, sum_van, tol=1.0)

    # ------------------------------------------------------------------
    # 6) CHAIN INTEGRITY -- "if yesterday van=2000 and sold=1500,
    #    today opening should be 500"
    # ------------------------------------------------------------------
    header("6. Chain integrity: leftover[d] == opening[d+1]  (per item, route 9108)")

    chain = db.execute_query(
        "WITH c AS ("
        "  SELECT trx_date, item_code, "
        "    ISNULL(opening_stock,0) AS our_open, "
        "    ISNULL(total_van_load,0) AS our_van, "
        "    ISNULL(actual_sold,0) AS sold, "
        "    ISNULL(leftover_to_next_day,0) AS our_left, "
        "    LAG(ISNULL(leftover_to_next_day,0),1) "
        "      OVER (PARTITION BY item_code ORDER BY trx_date) AS prev_left, "
        "    LAG(trx_date,1) OVER (PARTITION BY item_code ORDER BY trx_date) AS prev_date "
        "  FROM [YaumiAIML].[dbo].[yf_sales_transactions] "
        f"  WHERE route_code='{ROUTE}' AND trx_date BETWEEN '{START}' AND '{END}'"
        ") "
        "SELECT COUNT(*) AS pairs, "
        "  SUM(CASE WHEN DATEDIFF(day, prev_date, trx_date)=1 THEN 1 ELSE 0 END) AS contig, "
        "  SUM(CASE WHEN DATEDIFF(day, prev_date, trx_date)=1 "
        "       AND ABS(prev_left - our_open) < 0.001 THEN 1 ELSE 0 END) AS chain_ok "
        "FROM c WHERE prev_date IS NOT NULL;",
        db="aiml",
    ).iloc[0]
    print(f"  adjacent_pairs: {int(chain['pairs'])}  "
          f"contig: {int(chain['contig'])}  "
          f"chain_ok: {int(chain['chain_ok'])}")
    assert_eq("100% per-item chain identity",
              int(chain["chain_ok"]), int(chain["contig"]), tol=0)

    # Worked example: pick a specific item where prev_van > prev_sold > 0
    # and verify the literal "leftover = van - sold" identity per row.
    print()
    print("  Worked example -- one item, day-by-day:")
    sample = db.execute_query(
        "SELECT TOP 7 "
        "  trx_date, "
        "  ISNULL(opening_stock,0) AS opening, "
        "  ISNULL(fresh_load,0) AS fresh, "
        "  ISNULL(total_van_load,0) AS van, "
        "  ISNULL(actual_sold,0) AS sold, "
        "  ISNULL(leftover_to_next_day,0) AS leftover "
        "FROM [YaumiAIML].[dbo].[yf_sales_transactions] "
        f"WHERE route_code='{ROUTE}' AND item_code='50-0730' "
        f"  AND trx_date BETWEEN '{START}' AND '{END}' "
        "ORDER BY trx_date;",
        db="aiml",
    )
    print(f"    {'date':<12}{'open':>8}{'fresh':>8}{'van':>8}{'sold':>8}"
          f"{'leftover':>10}{'expected':>10}{'identity':>12}")
    prev_lo = None
    for _, r in sample.iterrows():
        v = float(r["van"])
        s = float(r["sold"])
        lo = float(r["leftover"])
        expected = max(0.0, v - s)
        chain_match = ""
        if prev_lo is not None:
            chain_match = " (prev_left -> opening: "
            chain_match += "MATCH)" if abs(prev_lo - float(r["opening"])) < 0.05 else "BREAK)"
        identity = "OK" if abs(expected - lo) < 0.05 else "NO"
        print(f"    {str(r['trx_date']):<12}{float(r['opening']):>8.1f}"
              f"{float(r['fresh']):>8.1f}{v:>8.1f}{s:>8.1f}{lo:>10.1f}"
              f"{expected:>10.1f}{identity:>12}{chain_match}")
        prev_lo = lo

    # ------------------------------------------------------------------
    # 8) HARD CHAIN TEST -- all 12 routes x 14 working days x per item
    #    "Last day van=X, sold=Y -> today opening = max(0, X-Y)"
    # ------------------------------------------------------------------
    header("7. Hard chain test: ALL routes, last 14 working days, per item")

    # Pull every (route, item, day, prev-day) tuple and verify the
    # identity literally: today's opening_stock equals max(0, prev_van -
    # prev_sold) from the prior CONSECUTIVE day this item appeared.
    full = db.execute_query(
        "WITH c AS ("
        "  SELECT trx_date, route_code, item_code,"
        "    ISNULL(opening_stock,0)        AS our_open,"
        "    ISNULL(total_van_load,0)       AS our_van,"
        "    ISNULL(actual_sold,0)          AS sold,"
        "    ISNULL(leftover_to_next_day,0) AS our_left,"
        "    LAG(ISNULL(total_van_load,0),1) "
        "      OVER (PARTITION BY route_code, item_code ORDER BY trx_date) AS prev_van,"
        "    LAG(ISNULL(actual_sold,0),1) "
        "      OVER (PARTITION BY route_code, item_code ORDER BY trx_date) AS prev_sold,"
        "    LAG(ISNULL(leftover_to_next_day,0),1) "
        "      OVER (PARTITION BY route_code, item_code ORDER BY trx_date) AS prev_left,"
        "    LAG(trx_date,1) "
        "      OVER (PARTITION BY route_code, item_code ORDER BY trx_date) AS prev_date"
        "  FROM [YaumiAIML].[dbo].[yf_sales_transactions]"
        "  WHERE trx_date BETWEEN DATEADD(day,-14,'2026-05-13') AND '2026-05-13'"
        "),"
        " checked AS ("
        "  SELECT *,"
        "    CASE WHEN prev_van > prev_sold THEN prev_van - prev_sold ELSE 0 END AS expected_leftover_naive"
        "  FROM c WHERE prev_date IS NOT NULL"
        " )"
        "SELECT route_code,"
        "  COUNT(*) AS pairs,"
        "  SUM(CASE WHEN DATEDIFF(day, prev_date, trx_date) = 1 THEN 1 ELSE 0 END) AS contig,"
        "  SUM(CASE WHEN DATEDIFF(day, prev_date, trx_date) = 1 "
        "       AND ABS(prev_left - our_open) < 0.001 THEN 1 ELSE 0 END) AS chain_match,"
        "  SUM(CASE WHEN DATEDIFF(day, prev_date, trx_date) = 1 "
        "       AND ABS(expected_leftover_naive - our_open) < 0.001 THEN 1 ELSE 0 END) AS naive_x_minus_y_match,"
        "  SUM(CASE WHEN DATEDIFF(day, prev_date, trx_date) = 1 "
        "       AND prev_van > prev_sold AND prev_sold > 0 THEN 1 ELSE 0 END) AS no_truck_cap_pairs"
        " FROM checked"
        " GROUP BY route_code"
        " ORDER BY route_code;",
        db="aiml",
    )
    print(f"  {'route':<8}{'pairs':>8}{'contig':>9}{'chain_ok':>11}{'X-Y_ok':>10}{'(non-cap)':>11}")
    tot = {"pairs": 0, "contig": 0, "chain_match": 0, "naive_x_minus_y_match": 0, "no_truck_cap_pairs": 0}
    for _, r in full.iterrows():
        for k in tot:
            tot[k] += int(r[k])
        print(f"  {str(r['route_code']):<8}"
              f"{int(r['pairs']):>8}"
              f"{int(r['contig']):>9}"
              f"{int(r['chain_match']):>11}"
              f"{int(r['naive_x_minus_y_match']):>10}"
              f"{int(r['no_truck_cap_pairs']):>11}")
    print(f"  {'-'*8}{'-'*8}{'-'*9}{'-'*11}{'-'*10}{'-'*11}")
    print(f"  {'TOTAL':<8}{tot['pairs']:>8}{tot['contig']:>9}{tot['chain_match']:>11}"
          f"{tot['naive_x_minus_y_match']:>10}{tot['no_truck_cap_pairs']:>11}")
    assert_eq("Chain identity (prev_left == today_open) across fleet",
              tot["chain_match"], tot["contig"], tol=0)
    # Naive X-Y check: per-item, when prev did NOT hit truck cap
    # (prev_van > prev_sold), today's opening must equal prev_van - prev_sold
    # exactly. When truck-capped (prev_van <= prev_sold), opening = 0
    # which is also handled by max(0, x-y). So naive_x_minus_y_match
    # should equal contig (every contiguous pair satisfies the rule
    # for both directions).
    assert_eq("Naive X-Y identity (today_open == max(0, prev_van - prev_sold))",
              tot["naive_x_minus_y_match"], tot["contig"], tol=0)

    # Worked example: pull 3 routes x 3 items showing the literal X-Y
    print()
    print("  Worked examples -- 'prev_van - prev_sold' becomes today's opening:")
    sample = db.execute_query(
        "WITH c AS ("
        "  SELECT trx_date, route_code, item_code,"
        "    ISNULL(opening_stock,0) AS our_open,"
        "    LAG(ISNULL(total_van_load,0),1) "
        "      OVER (PARTITION BY route_code, item_code ORDER BY trx_date) AS prev_van,"
        "    LAG(ISNULL(actual_sold,0),1) "
        "      OVER (PARTITION BY route_code, item_code ORDER BY trx_date) AS prev_sold,"
        "    LAG(trx_date,1) "
        "      OVER (PARTITION BY route_code, item_code ORDER BY trx_date) AS prev_date"
        "  FROM [YaumiAIML].[dbo].[yf_sales_transactions]"
        "  WHERE trx_date BETWEEN '2026-05-09' AND '2026-05-13'"
        "    AND route_code IN ('9105','9108','9209')"
        ") SELECT TOP 12 route_code, item_code, "
        "    CAST(prev_date AS NVARCHAR(10)) AS prev_date,"
        "    CAST(trx_date AS NVARCHAR(10)) AS today,"
        "    CAST(prev_van AS DECIMAL(10,1)) AS prev_van,"
        "    CAST(prev_sold AS DECIMAL(10,1)) AS prev_sold,"
        "    CAST(prev_van - prev_sold AS DECIMAL(10,1)) AS naive_xy,"
        "    CAST(our_open AS DECIMAL(10,1)) AS today_open"
        " FROM c WHERE prev_date IS NOT NULL "
        "   AND DATEDIFF(day, prev_date, trx_date) = 1"
        "   AND prev_van > 0 AND prev_sold > 0"
        " ORDER BY prev_van DESC;",
        db="aiml",
    )
    print(f"    {'route':<7}{'item':<13}{'prev_date':<12}{'today':<12}"
          f"{'prev_van':>10}{'prev_sold':>11}{'X-Y':>8}{'opens_with':>12}{'ok?':>5}")
    for _, r in sample.iterrows():
        ok = "Y" if abs(float(r["naive_xy"]) - float(r["today_open"])) < 0.05 or (
            float(r["naive_xy"]) <= 0 and float(r["today_open"]) == 0
        ) else "N"
        print(f"    {str(r['route_code']):<7}{str(r['item_code']):<13}"
              f"{str(r['prev_date']):<12}{str(r['today']):<12}"
              f"{float(r['prev_van']):>10.1f}{float(r['prev_sold']):>11.1f}"
              f"{float(r['naive_xy']):>8.1f}{float(r['today_open']):>12.1f}{ok:>5}")

    # ------------------------------------------------------------------
    # Tally
    # ------------------------------------------------------------------
    header("RESULT TALLY")
    pf = sum(1 for _, ok, *_ in results if ok)
    fl = sum(1 for _, ok, *_ in results if not ok)
    print(f"  TOTAL: {pf} PASS / {fl} FAIL out of {len(results)}")
    if fl:
        print("  FAILED:")
        for name, ok, got, exp in results:
            if not ok:
                print(f"    - {name}: got={got!r} vs expect={exp!r}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
