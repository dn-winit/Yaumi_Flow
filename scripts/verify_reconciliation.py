"""Cross-source regression matrix for the reconciliation chain.

Runs six independent invariants over a sample of (route, date) cells:

  1. ``sales_transactions.csv`` mirror equals ``yf_sales_transactions`` DB
  2. ``/reconciliation/van-load`` API matches the DB rep-side sum
  3. ``/reconciliation/past-performance`` items[] rows sum to the totals fields
  4. DB carry: ``yaumi_leftover[d-1] == yaumi_opening_stock[d]`` (with tolerance
     for forward-fill on items the rep posts sparsely)
  5. Supervision ``/session/saved`` endpoint is healthy per route
  6. Cross-view carry: Past Performance ``actual_leftover[d]`` equals Van Load
     ``past_leftover[d+1]`` -- the user-visible carry identity that broke when
     the uniform activity predicate was inconsistent across surfaces

Default scope:
  * 12 configured live routes (matches ``df.live_route_codes``)
  * 5 dates spanning the last 30 days: today, T-1, T-7, T-14, T-30

Exit codes:
  * 0 -- every check passed
  * 1 -- one or more FAIL (use this in CI / post-cron gates)
  * 2 -- WARN-only (degraded but not broken; advisory)

Usage:
    python scripts/verify_reconciliation.py                # default scope
    python scripts/verify_reconciliation.py --strict       # WARN counts as FAIL
    python scripts/verify_reconciliation.py --routes 9105 9108
    python scripts/verify_reconciliation.py --dates 2026-05-18 2026-05-17
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pyodbc
from dotenv import load_dotenv

# Make the project root importable so ``from common.route_registry``
# resolves whether the script is run as ``python scripts/...py`` (where
# Python's default path[0] is ``scripts/``) or via the runtime that
# already cwd's to the repo root. Same shim every other script in this
# folder uses.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

load_dotenv(Path(".env"))

# Default route fleet -- sourced from the shared registry so this
# script, the daily cron, and recommended_order all run against the
# same list. Override via the ``YF_ROUTE_CODES`` env var (read by the
# registry) without touching any code.
from common.route_registry import get_route_codes as _get_route_codes  # noqa: E402

DEFAULT_ROUTES = _get_route_codes()

# Tolerances tuned to "real drift vs rounding floor". Tighter than the
# original draft (5% / 2%) so a per-route 49-unit drift no longer hides.
# The forward-fill carry check needs the wider band because a route that
# rolled stale items across many days can accumulate drift legitimately.
TOL_CARRY_MAX_ABS = 25      # carry identity yaumi_leftover[d-1] == yaumi_opening_stock[d]
TOL_CARRY_PCT = 0.005       # 0.5% relative
TOL_CROSS_VIEW_MAX_ABS = 5  # cross-view carry diff in units
TOL_CROSS_VIEW_PCT = 0.002  # 0.2% relative

DEFAULT_TIMEOUT = 60


def _cs(prefix: str) -> str:
    """ODBC connection string for one of the configured DB credentials."""
    driver = os.getenv(prefix + "_DRIVER", "{ODBC Driver 17 for SQL Server}")
    host = os.getenv(prefix + "_HOST")
    port = os.getenv(prefix + "_PORT", "1433")
    db = os.getenv(prefix + "_DATABASE")
    user = os.getenv(prefix + "_USERNAME")
    pwd = os.getenv(prefix + "_PASSWORD")
    if not (host and user and pwd):
        raise SystemExit(f"missing {prefix}_* env vars in .env")
    return (
        f"DRIVER={driver};SERVER={host},{port};DATABASE={db};"
        f"UID={user};PWD={pwd};TrustServerCertificate=yes;Connection Timeout=30;"
    )


def _http_get_json(url: str):
    with urllib.request.urlopen(url, timeout=DEFAULT_TIMEOUT) as r:
        return json.load(r)


class Tally:
    """Counts PASS / WARN / FAIL and emits per-check lines."""

    def __init__(self) -> None:
        self.counts = {"PASS": 0, "WARN": 0, "FAIL": 0}

    def emit(self, test: str, route: str, when: str, status: str, detail: str = "") -> None:
        self.counts[status] = self.counts.get(status, 0) + 1
        print(f"  [{status}] {test:<26} {route} {when}  {detail}")


def _within_tol(diff: int, base: int, max_abs: int, pct: float) -> bool:
    return abs(diff) <= max(max_abs, int(abs(base) * pct))


# ----------------------------------------------------------------------
# Test bodies
# ----------------------------------------------------------------------

def test_csv_equals_db(t: Tally, routes: list[str], dates: list[str]) -> None:
    print("=" * 95)
    print("TEST 1: sales_transactions.csv mirror equals yf_sales_transactions DB")
    print("=" * 95)
    csv_path = Path(os.getenv("YF_DATA_ROOT", "data")) / "imports" / "sales_transactions.csv"
    if not csv_path.exists():
        for r in routes:
            for d in dates:
                t.emit("csv_eq_db", r, d, "FAIL", f"CSV not found at {csv_path}")
        return
    csv_df = pd.read_csv(csv_path)
    csv_df["TrxDate"] = csv_df["TrxDate"].astype(str).str[:10]
    csv_df["RouteCode"] = csv_df["RouteCode"].astype(str)

    with pyodbc.connect(_cs("DI_AIML_DB")) as conn:
        cur = conn.cursor()
        for r in routes:
            for d in dates:
                sub = csv_df[(csv_df.RouteCode == r) & (csv_df.TrxDate == d)]
                csv_load = int(sub["YaumiTotalVanLoad"].fillna(0).sum())
                csv_lo = int(sub["YaumiLeftover"].fillna(0).sum())
                csv_sold = int(sub["ActualSold"].fillna(0).sum())
                csv_rows = len(sub)
                cur.execute(
                    """SELECT COUNT(*), ISNULL(SUM(yaumi_total_van_load),0),
                              ISNULL(SUM(yaumi_leftover),0), ISNULL(SUM(actual_sold),0)
                       FROM [YaumiAIML].[dbo].[yf_sales_transactions]
                       WHERE route_code=? AND trx_date=?""",
                    (r, d),
                )
                db_rows, db_load, db_lo, db_sold = (int(x or 0) for x in cur.fetchone())
                if (csv_rows == db_rows and csv_load == db_load
                        and csv_lo == db_lo and csv_sold == db_sold):
                    t.emit("csv_eq_db", r, d, "PASS",
                           f"rows={csv_rows} load={csv_load} lo={csv_lo} sold={csv_sold}")
                else:
                    t.emit("csv_eq_db", r, d, "FAIL",
                           f"CSV({csv_rows}/{csv_load}/{csv_lo}/{csv_sold}) != "
                           f"DB({db_rows}/{db_load}/{db_lo}/{db_sold})")


def test_van_load_api_matches_db(t: Tally, routes: list[str], dates: list[str]) -> None:
    print()
    print("=" * 95)
    print("TEST 2: /reconciliation/van-load API matches DB yaumi_total_van_load")
    print("=" * 95)
    with pyodbc.connect(_cs("DI_AIML_DB")) as conn:
        cur = conn.cursor()
        for r in routes:
            for d in dates:
                try:
                    api = _http_get_json(
                        f"http://localhost:8002/api/v1/forecast/reconciliation/"
                        f"van-load?route_code={r}&date={d}"
                    )
                    if not api.get("available"):
                        t.emit("van_load_api_eq_db", r, d, "WARN", "api unavailable")
                        continue
                    api_van = int((api.get("totals") or {}).get("van_load_total", 0))
                    cur.execute(
                        """SELECT ISNULL(SUM(yaumi_total_van_load),0)
                           FROM [YaumiAIML].[dbo].[yf_sales_transactions]
                           WHERE route_code=? AND trx_date=?""",
                        (r, d),
                    )
                    db_van = int(cur.fetchone()[0])
                    if api_van == db_van:
                        t.emit("van_load_api_eq_db", r, d, "PASS",
                               f"api={api_van} db={db_van}")
                    else:
                        t.emit("van_load_api_eq_db", r, d, "FAIL",
                               f"diff {api_van - db_van}")
                except Exception as e:
                    t.emit("van_load_api_eq_db", r, d, "FAIL",
                           f"{type(e).__name__}: {str(e)[:60]}")


def test_past_performance_identity(t: Tally, routes: list[str], dates: list[str]) -> None:
    print()
    print("=" * 95)
    print("TEST 3: past-performance items[] sums equal totals fields")
    print("=" * 95)
    for r in routes:
        for d in dates:
            try:
                api = _http_get_json(
                    f"http://localhost:8002/api/v1/forecast/reconciliation/"
                    f"past-performance?route_code={r}&start_date={d}&end_date={d}"
                )
                items = api.get("items") or []
                totals = api.get("totals") or {}
                item_van = sum(it.get("rep_van_load", 0) for it in items)
                item_sold = sum(it.get("actual_sold", 0) for it in items)
                item_lo = sum(it.get("actual_leftover", 0) for it in items)
                t_van = totals.get("rep_van_load_total", 0) or 0
                t_sold = totals.get("actual_sold_total", 0) or 0
                t_lo = totals.get("rep_leftover_units", 0) or 0
                if (abs(item_van - t_van) < 1
                        and abs(item_sold - t_sold) < 1
                        and abs(item_lo - t_lo) < 1):
                    t.emit("past_perf_identity", r, d, "PASS",
                           f"items={len(items)} van={int(item_van)} sold={int(item_sold)} lo={int(item_lo)}")
                else:
                    t.emit("past_perf_identity", r, d, "FAIL",
                           f"item({int(item_van)}/{int(item_sold)}/{int(item_lo)}) != "
                           f"totals({t_van}/{t_sold}/{t_lo})")
            except Exception as e:
                t.emit("past_perf_identity", r, d, "FAIL",
                       f"{type(e).__name__}: {str(e)[:60]}")


def test_db_carry_identity(t: Tally, routes: list[str], dates: list[str]) -> None:
    print()
    print("=" * 95)
    print("TEST 4: DB carry yaumi_leftover[d-1] ~ yaumi_opening_stock[d]")
    print("=" * 95)
    with pyodbc.connect(_cs("DI_AIML_DB")) as conn:
        cur = conn.cursor()
        for r in routes:
            for d in dates:
                prev = (date.fromisoformat(d) - timedelta(days=1)).isoformat()
                cur.execute(
                    """SELECT ISNULL(SUM(yaumi_leftover),0)
                       FROM [YaumiAIML].[dbo].[yf_sales_transactions]
                       WHERE route_code=? AND trx_date=?""",
                    (r, prev),
                )
                prev_lo = int(cur.fetchone()[0])
                cur.execute(
                    """SELECT ISNULL(SUM(yaumi_opening_stock),0)
                       FROM [YaumiAIML].[dbo].[yf_sales_transactions]
                       WHERE route_code=? AND trx_date=?""",
                    (r, d),
                )
                today_open = int(cur.fetchone()[0])
                diff = today_open - prev_lo
                status = ("PASS" if _within_tol(diff, prev_lo, TOL_CARRY_MAX_ABS, TOL_CARRY_PCT)
                          else "WARN")
                t.emit("carry_identity_db", r, d, status,
                       f"leftover[{prev}]={prev_lo} opening[{d}]={today_open} diff={diff:+d}")


def test_supervision_endpoint(t: Tally, routes: list[str], date_for_supervision: str) -> None:
    print()
    print("=" * 95)
    print(f"TEST 5: supervision /session/saved healthy for each route ({date_for_supervision})")
    print("=" * 95)
    for r in routes:
        try:
            sup = _http_get_json(
                f"http://localhost:8004/api/v1/supervision/session/saved"
                f"?route_code={r}&date={date_for_supervision}"
            )
            t.emit("supervision_saved", r, date_for_supervision, "PASS",
                   f"available={sup.get('available')} visits={len(sup.get('visits') or {})} "
                   f"briefings={len(sup.get('briefings') or {})}")
        except Exception as e:
            t.emit("supervision_saved", r, date_for_supervision, "FAIL",
                   f"{type(e).__name__}: {str(e)[:60]}")


def test_cross_view_carry(t: Tally, routes: list[str], dates: list[str]) -> None:
    print()
    print("=" * 95)
    print("TEST 6: cross-view carry -- PP actual_leftover[d] == VL past_leftover[d+1]")
    print("=" * 95)
    for r in routes:
        for d in dates:
            try:
                pp = _http_get_json(
                    f"http://localhost:8002/api/v1/forecast/reconciliation/"
                    f"past-performance?route_code={r}&start_date={d}&end_date={d}"
                )
                pp_lo = sum(it.get("actual_leftover", 0) for it in (pp.get("items") or []))
                d_next = (date.fromisoformat(d) + timedelta(days=1)).isoformat()
                vl = _http_get_json(
                    f"http://localhost:8002/api/v1/forecast/reconciliation/"
                    f"van-load?route_code={r}&date={d_next}"
                )
                vl_past = (vl.get("totals") or {}).get("past_leftover_total", 0)
                diff = int(vl_past) - int(pp_lo)
                status = ("PASS" if _within_tol(diff, int(pp_lo), TOL_CROSS_VIEW_MAX_ABS, TOL_CROSS_VIEW_PCT)
                          else "WARN")
                t.emit("carry_cross_view", r, f"{d}->{d_next}", status,
                       f"PP lo[{d}]={int(pp_lo)} VL past_lo[{d_next}]={int(vl_past)} diff={diff:+d}")
            except Exception as e:
                t.emit("carry_cross_view", r, d, "FAIL",
                       f"{type(e).__name__}: {str(e)[:60]}")


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def _default_dates(today: date) -> list[str]:
    """Today, T-1, T-7, T-14, T-30 -- spans the daily cron's 30-day horizon."""
    offsets = [0, 1, 7, 14, 30]
    return [(today - timedelta(days=o)).isoformat() for o in offsets]


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the reconciliation cross-source regression matrix.",
    )
    parser.add_argument(
        "--routes", nargs="*", default=DEFAULT_ROUTES,
        help="Routes to test (default: 12 live routes)",
    )
    parser.add_argument(
        "--dates", nargs="*", default=None,
        help="Dates (YYYY-MM-DD) to test. Default: today, T-1, T-7, T-14, T-30.",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero on any WARN as well as FAIL.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    today = date.today()
    dates = args.dates if args.dates else _default_dates(today)
    routes = args.routes
    supervision_date = today.isoformat()

    tally = Tally()
    test_csv_equals_db(tally, routes, dates)
    test_van_load_api_matches_db(tally, routes, dates)
    test_past_performance_identity(tally, routes, dates)
    test_db_carry_identity(tally, routes, dates)
    test_supervision_endpoint(tally, routes, supervision_date)
    test_cross_view_carry(tally, routes, dates)

    print()
    print("=" * 95)
    print("SUMMARY")
    print("=" * 95)
    total = sum(tally.counts.values())
    for status in ("PASS", "WARN", "FAIL"):
        print(f"  {status}: {tally.counts.get(status, 0)}")
    print(f"  Total: {total}")

    if tally.counts.get("FAIL", 0) > 0:
        return 1
    if args.strict and tally.counts.get("WARN", 0) > 0:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
