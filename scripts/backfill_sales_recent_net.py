"""
One-shot backfill: re-pull ``sales_recent`` with return-netting applied,
route-by-route, gentle on YaumiLive.

Why this exists
---------------
The new ``sales_recent`` SQL nets returns against their original invoice
via ``ReturnItem_InvoiceRef`` (see data_import/core/queries.py). The
existing ``sales_recent.csv`` was produced by the OLD SQL before that
change, so its ``TotalQuantity`` column is still gross. To replace the
CSV with return-adjusted values across the full training window
(typically 5 years) without slamming YaumiLive, this script:

  * Loads one route's 5 years of sales at a time (settings.route_codes
    iteration, configurable subset via --routes).
  * Calls the existing ``import_dataset`` path with ``lookback_days``
    set to the full backfill window, so the importer's natural-key
    dedup-merge handles per-route accumulation in the CSV.
  * Sleeps a configurable interval between routes so the OLTP server
    sees a sustained-but-light load instead of one big burst.

Each per-route query touches ~1/N of the fleet's data (12 routes today).
The merge step keeps last-write-wins on
``(RouteCode, CustomerCode, ItemCode, TrxDate)``, so rows for routes
not in this iteration are untouched.

Resumable
---------
Re-running after an interruption is safe. The merge is idempotent on
the natural key; routes already processed will simply re-overwrite
themselves with the same net values. To skip already-completed routes
within the same session, the script prints a route checkpoint file
(``backfill_progress.txt``) it can resume from on the next run.

Usage
-----
    python scripts/backfill_sales_recent_net.py
        [--routes R001,R002,...]      # subset; default = settings.route_codes
        [--lookback-days 1825]        # default = 5 years
        [--sleep-seconds 5]           # delay between routes; default 5s
        [--resume]                    # skip routes already in backfill_progress.txt
        [--dry-run]                   # log the SQL/params, don't run it

Honest cost estimate
--------------------
With 12 routes and a 5-second sleep, the wall-clock overhead from
sleeps alone is 12 * 5 = 60 seconds. The actual queries dominate
total runtime; each route's 5-year pull on a moderate fleet runs
in tens of seconds to a few minutes depending on row count and
YaumiLive load. Plan on 10-30 minutes total.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# Anchor sys.path to the repo root so ``python scripts/...`` works
# from any cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Load .env into os.environ BEFORE importing settings. pydantic-settings'
# ``_env_file`` arg only applies to the top-level Settings model -- nested
# DatabaseSettings models read their own ``DI_DB_*`` keys directly from
# os.environ, so they need the file resolved into the process environment.
# Matches the pattern in data_import/__main__.py.
from dotenv import load_dotenv  # noqa: E402
load_dotenv(_REPO_ROOT / ".env")

from data_import.config.settings import get_settings  # noqa: E402
from data_import.core.importer import DataImporter   # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill")

PROGRESS_FILE = _REPO_ROOT / "scripts" / ".backfill_sales_recent_progress.txt"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--routes", default=None,
                   help="Comma-separated route subset; default = settings.route_codes")
    p.add_argument("--lookback-days", type=int, default=1825,
                   help="Days of history per route. Default 1825 = 5 years.")
    p.add_argument("--sleep-seconds", type=float, default=5.0,
                   help="Sleep between routes to be gentle on YaumiLive. Default 5s.")
    p.add_argument("--resume", action="store_true",
                   help="Skip routes already listed in the progress file.")
    p.add_argument("--dry-run", action="store_true",
                   help="Don't run queries; just print the plan and exit.")
    return p.parse_args()


def _load_progress() -> set[str]:
    if not PROGRESS_FILE.exists():
        return set()
    return {ln.strip() for ln in PROGRESS_FILE.read_text().splitlines() if ln.strip()}


def _append_progress(route: str) -> None:
    with PROGRESS_FILE.open("a", encoding="utf-8") as fh:
        fh.write(f"{route}\n")


def main() -> int:
    args = _parse_args()
    settings = get_settings()

    # Decide which routes to process.
    if args.routes:
        target_routes = [r.strip() for r in args.routes.split(",") if r.strip()]
    else:
        target_routes = list(settings.route_codes)

    if not target_routes:
        logger.error("No routes to process (settings.route_codes empty and --routes not set)")
        return 2

    done = _load_progress() if args.resume else set()
    pending = [r for r in target_routes if r not in done]

    logger.info(
        "Backfill plan: %d route(s) over %d days (%.1fy), %.1fs sleep between routes, "
        "%d already done in prior runs",
        len(pending), args.lookback_days, args.lookback_days / 365.0,
        args.sleep_seconds, len(done),
    )
    if args.resume and done:
        logger.info("Resuming -- skipping: %s", ", ".join(sorted(done)))

    if args.dry_run:
        logger.info("DRY-RUN: would call import_dataset(sales_recent, "
                    "mode=incremental, lookback_days=%d, routes=[<one>]) for %d routes",
                    args.lookback_days, len(pending))
        for i, r in enumerate(pending, 1):
            logger.info("  [%2d/%d] %s", i, len(pending), r)
        return 0

    importer = DataImporter(settings)
    overall_start = time.time()
    total_new_rows = 0
    total_rows_seen = 0
    failures: list[tuple[str, str]] = []

    for idx, route in enumerate(pending, 1):
        elapsed = time.time() - overall_start
        logger.info(
            "[%d/%d] route=%s  elapsed=%.1fs", idx, len(pending), route, elapsed,
        )
        t0 = time.time()
        try:
            result = importer.import_dataset(
                "sales_recent",
                mode="incremental",
                lookback_days=int(args.lookback_days),
                routes=[route],
            )
        except Exception as exc:
            logger.error("  FAILED route=%s: %s", route, exc, exc_info=True)
            failures.append((route, str(exc)))
            # Don't sleep on failure; move to next route immediately.
            continue

        dt = time.time() - t0
        if result.get("success"):
            new_rows = int(result.get("new_rows", 0))
            total_rows = int(result.get("total_rows", 0))
            total_new_rows += new_rows
            total_rows_seen = total_rows  # CSV row count after this merge
            logger.info(
                "  OK route=%s  +%d new rows, CSV now %d rows  (%.1fs query)",
                route, new_rows, total_rows, dt,
            )
            _append_progress(route)
        else:
            err = result.get("error", "unknown")
            logger.error("  FAILED route=%s: %s  (%.1fs)", route, err, dt)
            failures.append((route, err))

        # Gentle pacing: sleep AFTER each route except the last.
        if idx < len(pending) and args.sleep_seconds > 0:
            time.sleep(args.sleep_seconds)

    total_elapsed = time.time() - overall_start
    logger.info(
        "Backfill complete: %d route(s) processed, %d failed, +%d total new rows, "
        "CSV now %d rows, %.1fs wall-clock",
        len(pending) - len(failures), len(failures),
        total_new_rows, total_rows_seen, total_elapsed,
    )
    if failures:
        logger.error("Failed routes (re-run with --resume to retry just these):")
        for route, err in failures:
            logger.error("  %s: %s", route, err)
        return 1

    # All routes processed successfully -- clear the progress file so a
    # future backfill starts clean.
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()
        logger.info("Progress file cleared.")

    logger.info("Next training run will read return-adjusted TotalQuantity. "
                "The 03:00 cron will keep the rolling last %d days re-netted "
                "as late returns arrive.", settings.sales_recent_refresh_window_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
