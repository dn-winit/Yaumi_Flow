"""Page-view endpoints; one HTTP fetch per page state.

All aggregation/sort/filter lives here (single source of truth, byte-consistent).
The webapp is a render layer. ASCII-only output.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import pandas as pd
from fastapi import APIRouter, Depends, Query

from common.db_pool import get_pool
from common.numeric import safe_float as _to_float
from demand_forecasting_pipeline.api.dependencies import (
    get_artifact_service,
    get_van_load_service,
)
from demand_forecasting_pipeline.api.routes.predictions import _detect_predicted_col
from demand_forecasting_pipeline.api.schemas import (
    ForecastDrawerChartPoint,
    ForecastDrawerSummary,
    ForecastDrawerTableRow,
    ForecastDrawerView,
    VanLoadChartItem,
    VanLoadPageView,
    VanLoadPageViewItem,
    VanLoadSummaryView,
    VanLoadTableRow,
)
from demand_forecasting_pipeline.config.settings import get_settings
from demand_forecasting_pipeline.services.artifact_service import ArtifactService
from demand_forecasting_pipeline.services.reconciliation.enrich import (
    _concentrated_buyers_index,
    _journey_index,
)
from demand_forecasting_pipeline.services.reconciliation.van_load_service import VanLoadService

logger = logging.getLogger(__name__)

_DATE_RE = r"^\d{4}-\d{2}-\d{2}$"


router = APIRouter(prefix="/page-views", tags=["page-views"])

# Real-probability classes; smooth/erratic emit synthetic 0/1 fallbacks (excluded from at_risk).
REAL_PROBABILITY_CLASSES = frozenset({"intermittent", "lumpy"})


def _guard_masked_items(route_code: str, date: str) -> frozenset[str]:
    """Items on (route, date) where the journey-aware concentration mask zeroes the load.
    Same inputs as enrich_with_load's mask; both indices mtime-cached."""
    s = get_settings()
    if not getattr(s, "concentration_guard_enabled", False):
        return frozenset()
    from pathlib import Path
    conc = _concentrated_buyers_index(
        Path(s.shared_data_dir) / s.customer_data_file,
        window_days=int(s.concentration_window_days),
        threshold=float(s.concentration_threshold),
        top_k=int(s.concentration_top_k),
        min_units=float(s.concentration_min_units),
    )
    if not conc:
        return frozenset()
    journey = _journey_index(Path(s.shared_data_dir) / s.journey_plan_file)
    day_journey = journey.get(str(date)) if journey else None
    if not day_journey:
        return frozenset()
    route_journey = day_journey.get(str(route_code))
    if route_journey is None:
        return frozenset()
    masked = {
        item for (rt, item), whales in conc.items()
        if str(rt) == str(route_code) and whales.isdisjoint(route_journey)
    }
    return frozenset(masked)


# Helpers (route-local; page-view-shaping concerns only).


def _opt_float(x: object) -> float | None:
    """Coerce to float; preserve None for genuinely missing values."""
    if x is None:
        return None
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def _has_real_confidence(cls: str | None) -> bool:
    if not cls:
        return False
    return str(cls).strip().lower() in REAL_PROBABILITY_CLASSES


def _first_present(df: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    for n in names:
        if n in df.columns:
            return n
    return None


def _count_csv_rows_on_date(csv_path, column: str, target: str) -> int:
    """Count CSV rows where ``column`` equals ``target`` without materialising
    the file in pandas. Streams line-by-line; constant memory.

    Used by the staleness probe to compare CSV vs DB row counts for one
    date; a full ``pd.read_csv(... usecols=[col])`` of a 500MB mirror was
    the dominant cost on miss-path requests.
    """
    import csv as _csv
    with open(csv_path, encoding="utf-8", newline="") as fh:
        reader = _csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            return 0
        try:
            idx = header.index(column)
        except ValueError:
            return 0
        target_s = str(target)
        count = 0
        for row in reader:
            if idx < len(row) and row[idx] == target_s:
                count += 1
        return count


# Item catalog (price + name); mtime-keyed cache against sales_recent.csv.
_CATALOG_CACHE: dict[str, object] = {"mtime": 0.0, "by_item": {}}


# Cross-source staleness signal: row count on today's date (CSV vs DB).
# Content-based (not mtime-based) to avoid SQL Server TZ vs OS TZ ambiguity
# and to ignore cosmetic CSV rewrites. Mtime-cached: one DB probe per CSV revision.
_STALENESS_CACHE: dict[str, Any] = {
    "mtime": -1.0,
    "csv_today_rows": None,
    "db_today_rows": None,
}


def _log_sales_transactions_staleness(route_code: str, date: str) -> None:
    """WARN when sales_transactions CSV is missing date rows that exist in DB.

    Content-based (CSV row count vs DB row count); robust to TZ ambiguity and
    cosmetic CSV rewrites. Mtime-keyed: one DB probe per CSV revision.
    """
    s = get_settings()
    csv_path = s.shared_data_path(s.sales_transactions_file)
    if not csv_path.exists():
        return
    try:
        csv_mtime = csv_path.stat().st_mtime
    except OSError:
        return

    cached_mtime = _STALENESS_CACHE.get("mtime")
    if cached_mtime == csv_mtime:
        csv_today = _STALENESS_CACHE.get("csv_today_rows")
        db_today  = _STALENESS_CACHE.get("db_today_rows")
        if csv_today is None or db_today is None:
            return
    else:
        # Count CSV rows for date via line-streaming so a 500MB sales_transactions
        # mirror doesn't materialise in RAM just to compute one integer.
        # The CSV is comma-separated with TrxDate as the FIRST column; we scan
        # the header to locate it, then count lines whose first matching field
        # equals the target date. Falls back to pd.read_csv usecols if the
        # column order changes.
        try:
            csv_today = _count_csv_rows_on_date(csv_path, "TrxDate", str(date))
        except Exception as exc:
            logger.warning(
                "van_load_staleness_csv_count_failed route=%s date=%s err=%s",
                route_code, date, exc,
            )
            return
        try:
            # Use a tight (5s) connect timeout: this probe is a diagnostic
            # warning logger, not part of the response payload. The default
            # ``s.db.connection_timeout`` (120s) was blocking the UI for
            # 2 minutes when YaumiAIML was unreachable. Same for the
            # per-query budget -- the COUNT is a single row, sub-second
            # under normal load.
            probe_pool = get_pool(
                s.db.connection_string(),
                max_connections=max(int(s.db.retry_attempts) + 1, 4),
                connect_timeout=5,
                query_timeout=10,
                autocommit=True,
            )
            # CSV mirror restricted to live_route_codes; filter DB count likewise to avoid false positives.
            routes = list(getattr(s, "live_route_codes", []) or [])
            # Single source of truth -- the FQN constant lives in reconciliation_refresh
            # so a future rename touches one place.
            from demand_forecasting_pipeline.services.reconciliation_refresh import _SALES_TARGET_TABLE
            db_sql = (
                f"SELECT COUNT(*) FROM {_SALES_TARGET_TABLE} "
                "WITH (NOLOCK) WHERE trx_date = ?"
            )
            db_params: list = [str(date)]
            if routes:
                db_sql += f" AND route_code IN ({','.join(['?'] * len(routes))})"
                db_params.extend(str(r) for r in routes)
            db_sql += ";"
            with probe_pool.acquire() as conn:
                cur = conn.cursor()
                cur.execute(db_sql, db_params)
                db_today = int(cur.fetchone()[0])
        except Exception as exc:
            logger.warning(
                "van_load_staleness_probe_failed route=%s date=%s err=%s",
                route_code, date, exc,
            )
            return
        _STALENESS_CACHE["mtime"] = csv_mtime
        _STALENESS_CACHE["csv_today_rows"] = csv_today
        _STALENESS_CACHE["db_today_rows"]  = db_today

    # Fire only when DB > CSV (failed cascade); equal or CSV-more is benign.
    if db_today > csv_today:
        logger.warning(
            "sales_transactions_csv_missing_rows route=%s date=%s "
            "csv_rows=%d db_rows=%d gap=%d",
            route_code, date, csv_today, db_today, db_today - csv_today,
        )


def _load_item_catalog() -> dict[str, dict[str, object]]:
    """{ItemCode: {name, price}} from sales_recent.csv (data_import's source); mtime-cached."""
    s = get_settings()
    path = s.shared_data_path(s.sales_recent_file)
    if not path.exists():
        return {}
    mtime = path.stat().st_mtime
    if mtime == _CATALOG_CACHE["mtime"] and _CATALOG_CACHE["by_item"]:
        return _CATALOG_CACHE["by_item"]  # type: ignore[return-value]

    df = pd.read_csv(path)
    needed = {"ItemCode", "ItemName", "AvgUnitPrice"}
    if not needed.issubset(df.columns):
        _CATALOG_CACHE["mtime"] = mtime
        _CATALOG_CACHE["by_item"] = {}
        return {}

    df["ItemCode"] = df["ItemCode"].astype(str).str.strip()
    df["AvgUnitPrice"] = pd.to_numeric(df["AvgUnitPrice"], errors="coerce")

    # Mean price per item over window; last-name on rename (deterministic via groupby).
    has_category = "CategoryName" in df.columns
    agg_spec: dict[str, tuple[str, str]] = {
        "name":  ("ItemName",     "last"),
        "price": ("AvgUnitPrice", "mean"),
    }
    if has_category:
        agg_spec["category"] = ("CategoryName", "last")
    grouped = df.groupby("ItemCode").agg(**agg_spec)
    by_item: dict[str, dict[str, object]] = {}
    for code, row in grouped.iterrows():
        price = row["price"]
        entry: dict[str, object] = {
            "name": str(row["name"]) if pd.notna(row["name"]) else str(code),
            "price": float(price) if pd.notna(price) else None,
        }
        if has_category:
            cat = row["category"]
            entry["category"] = (
                str(cat).strip() if pd.notna(cat) and str(cat).strip() else ""
            )
        by_item[str(code)] = entry
    _CATALOG_CACHE["mtime"] = mtime
    _CATALOG_CACHE["by_item"] = by_item
    return by_item


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------


@router.get("/van-load", response_model=VanLoadPageView)
def van_load_page_view(
    route_code: str = Query(..., description="Route code, e.g. 9105"),
    date: str = Query(..., pattern=_DATE_RE, description="YYYY-MM-DD"),
    top_n: int = Query(
        10,
        ge=1,
        le=100,
        description="Cap for the 'Top N items by van load' chart slice",
    ),
    svc: ArtifactService = Depends(get_artifact_service),
    van_svc: VanLoadService = Depends(get_van_load_service),
) -> VanLoadPageView:
    """Composite payload for the VanLoad route-detail page (one frame snapshot).

    Reads enriched van-load frame, scopes to (route, date), reconciles via V5_b if
    DB values absent, computes carry-aware summary, top-N chart, table rows, and
    cross-checks the identity carried + issued == van_load_qty.
    """
    # WARN on CSV-vs-DB staleness; CSV stays the served source (ops signal only).
    _log_sales_transactions_staleness(str(route_code), str(date))

    # van_load_view_enriched: DB-stored when cron filled it, else lazily engine-computed and mtime-cached.
    fc_df = svc.van_load_view_enriched()
    if fc_df.empty:
        return VanLoadPageView(
            success=True,
            available=False,
            message="No forecast data available",
            route_code=route_code,
            date=date,
            summary=VanLoadSummaryView(),
        )

    pred_col = _detect_predicted_col(fc_df)
    if pred_col is None:
        return VanLoadPageView(
            success=True,
            available=False,
            message="Forecast frame has no prediction column",
            route_code=route_code,
            date=date,
            summary=VanLoadSummaryView(),
        )

    df = fc_df.copy()
    df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df[
        (df["RouteCode"].astype(str) == str(route_code))
        & (df["TrxDate"] == str(date))
    ]
    if df.empty:
        return VanLoadPageView(
            success=True,
            available=False,
            message=f"No forecast for route {route_code} on {date}",
            route_code=route_code,
            date=date,
            summary=VanLoadSummaryView(),
        )

    have_recon = "recommended_load" in df.columns

    if not have_recon:
        logger.error(
            "van_load_page_view_reconciliation_degraded route=%s date=%s",
            route_code,
            date,
        )

    # Canonicalise: one column per concept, no fallback chains downstream.
    if "recommended_load" in df.columns:
        df["_units_to_load"] = (
            pd.to_numeric(df["recommended_load"], errors="coerce").fillna(0.0)
        )
    else:
        df["_units_to_load"] = (
            pd.to_numeric(df[pred_col], errors="coerce").fillna(0.0)
        )

    df["_opening_stock"] = pd.to_numeric(
        df.get("opening_stock", 0.0), errors="coerce"
    ).fillna(0.0)

    lb_col = _first_present(df, ("load_lower_bound", "lower_bound", "q_10"))
    ub_col = _first_present(df, ("load_upper_bound", "upper_bound", "q_90"))
    df["_lower_bound"] = (
        pd.to_numeric(df[lb_col], errors="coerce")
        if lb_col
        else pd.Series([None] * len(df), index=df.index, dtype="object")
    )
    df["_upper_bound"] = (
        pd.to_numeric(df[ub_col], errors="coerce")
        if ub_col
        else pd.Series([None] * len(df), index=df.index, dtype="object")
    )

    df["_p_demand"] = pd.to_numeric(df.get("p_demand"), errors="coerce")

    cls_col = _first_present(df, ("class", "demand_class"))
    df["_class"] = (
        df[cls_col].astype(str).str.lower()
        if cls_col
        else pd.Series([""] * len(df), index=df.index)
    )

    df["_item_code"] = df["ItemCode"].astype(str)

    # Forecast frame's price column wins; sales-recent catalog as fallback.
    price_col = _first_present(
        df, ("AvgUnitPrice", "avg_unit_price", "unit_price")
    )
    if price_col:
        df["_price"] = pd.to_numeric(df[price_col], errors="coerce")
    else:
        df["_price"] = pd.Series([float("nan")] * len(df), index=df.index)

    name_col = _first_present(df, ("ItemName", "item_name"))
    if name_col:
        df["_item_name"] = df[name_col].astype(str)
    else:
        df["_item_name"] = df["_item_code"]

    catalog = _load_item_catalog()
    if catalog:
        # Fill missing prices/names from catalog; forecast frame wins when both have values.
        price_missing = df["_price"].isna()
        if price_missing.any():
            df.loc[price_missing, "_price"] = (
                df.loc[price_missing, "_item_code"]
                .map(lambda c: catalog.get(c, {}).get("price"))
                .astype("float64")
            )
        name_missing = (
            df["_item_name"].isna()
            | (df["_item_name"].astype(str).str.strip() == "")
            | (df["_item_name"].astype(str).str.lower() == "nan")
        )
        if name_missing.any():
            df.loc[name_missing, "_item_name"] = df.loc[
                name_missing, "_item_code"
            ].map(lambda c: str(catalog.get(c, {}).get("name") or c))

    # expected_demand = engine's final per-day target; recent_avg = historical anchor.
    # CSV mirror exposes PascalCase or snake_case; both accepted.
    _recent_avg_col = _first_present(
        df, ("recent_avg_per_selling_day", "RecentAvgPerSellingDay"),
    )
    df["_recent_avg_per_selling_day"] = (
        pd.to_numeric(df[_recent_avg_col], errors="coerce")
        if _recent_avg_col else pd.Series([float("nan")] * len(df), index=df.index)
    )
    _expected_col = _first_present(df, ("expected_demand", "ExpectedDemand"))
    df["_expected_demand"] = (
        pd.to_numeric(df[_expected_col], errors="coerce")
        if _expected_col else pd.Series([float("nan")] * len(df), index=df.index)
    )
    # Per-row sanity flag from the canonical mirror. Tolerates the CSV's
    # string serialisation of bool ("True"/"False") and the rename map's
    # snake_case form. Missing column surfaces as None so the frontend
    # can distinguish "diagnostic not yet populated" from a real False
    # ("guard didn't fire on a populated row").
    if "forecast_below_recent" in df.columns:
        _below_raw = df["forecast_below_recent"]
    elif "ForecastBelowRecent" in df.columns:
        _below_raw = df["ForecastBelowRecent"]
    else:
        _below_raw = None
    df["_forecast_below_recent"] = (
        _below_raw.astype(str).str.strip().str.lower().isin({"true", "1", "t", "yes"})
        if _below_raw is not None
        else pd.Series([None] * len(df), index=df.index, dtype=object)
    )
    # Journey-aware concentration guard: True when the row's load was
    # zeroed because the dominant buyer isn't on the day's journey plan.
    # Modal renders this as "skipped: top buyer not on today's plan".
    # Inferred at read time from the same mtime-cached helpers
    # ``enrich_with_load`` uses, so the popup tags exactly the rows the
    # cron's mask zeroed out -- no new DB column needed.
    _guard_set = _guard_masked_items(str(route_code), str(date))
    df["_guard_skipped"] = df["_item_code"].astype(str).isin(_guard_set) if _guard_set else False


    # Summary tile (carry-aware). _opening_stock/_units_to_load come from the enrich
    # idempotency overlay so DB-persisted rows match the cron's written values exactly.
    carry_mask = (df["_units_to_load"] > 0) | (df["_opening_stock"] > 0)
    carry_df = df.loc[carry_mask]

    # Tile aggregates use per-row pack_qty (same as table) for byte-consistent identity.
    from common.numeric import pack_qty
    carried_per_item = carry_df.loc[carry_df["_opening_stock"] > 0, "_opening_stock"].apply(pack_qty)
    issued_per_item  = carry_df.loc[carry_df["_units_to_load"] > 0, "_units_to_load"].apply(pack_qty)
    carried_qty = float(carried_per_item.sum())
    issued_qty  = float(issued_per_item.sum())
    carried_items = int(
        carry_df.loc[carry_df["_opening_stock"] > 0, "_item_code"].nunique()
    )
    issued_items = int(
        carry_df.loc[carry_df["_units_to_load"] > 0, "_item_code"].nunique()
    )
    van_load_items = int(carry_df["_item_code"].nunique())
    van_load_qty = carried_qty + issued_qty

    # Revenue from ceiled cells (whole packs * price); currency() rounds once at wire boundary.
    from common.numeric import currency
    load_df = df.loc[df["_units_to_load"] > 0]
    has_revenue = bool(load_df["_price"].notna().any())
    revenue_val = (
        float(
            (
                load_df["_units_to_load"].apply(pack_qty)
                * load_df["_price"].fillna(0.0)
            ).sum()
        )
        if has_revenue
        else None
    )
    at_risk = int(
        (
            load_df["_p_demand"].notna()
            & (load_df["_p_demand"] < float(get_settings().at_risk_prob_threshold))
            & load_df["_class"].isin(REAL_PROBABILITY_CLASSES)
        ).sum()
    )

    # Quantities are integer; round(x, 1) retained for wire-contract stability.
    summary = VanLoadSummaryView(
        van_load_qty=round(van_load_qty, 1),
        van_load_items=van_load_items,
        carried_qty=round(carried_qty, 1),
        carried_items=carried_items,
        issued_qty=round(issued_qty, 1),
        issued_items=issued_items,
        revenue=currency(revenue_val) if revenue_val is not None else None,
        has_revenue=has_revenue,
        at_risk=at_risk,
    )

    # Cross-check identity carried + issued == van_load_qty (delta > 1e-6 means masks drifted).
    identity_delta = abs((carried_qty + issued_qty) - van_load_qty)
    if identity_delta > 1e-6:
        logger.error(
            "van_load_summary_identity_violated route=%s date=%s "
            "carried=%s issued=%s sum=%s delta=%s",
            route_code,
            date,
            carried_qty,
            issued_qty,
            van_load_qty,
            identity_delta,
        )

    # ---- Chart top-N --------------------------------------------------
    chart_df = (
        load_df.sort_values("_units_to_load", ascending=False).head(int(top_n))
    )
    chart_top_n = [
        VanLoadChartItem(
            item_code=str(r["_item_code"]),
            item_name=str(r["_item_name"]),
            # Integer-ceil to match table per-SKU.
            predicted=float(pack_qty(r["_units_to_load"])),
        )
        for _, r in chart_df.iterrows()
    ]

    # Table (carry-aware): keep rows with any truck contribution (fresh, carry, or both).
    # Pure-carry rows are real inventory; guard-masked rows kept so the popup can explain
    # the zero. Sort by total truck weight.
    table_df = df.loc[
        (df["_units_to_load"] > 0)
        | (df["_opening_stock"] > 0)
        | df["_guard_skipped"].astype(bool)
    ].copy()
    from common.numeric import pack_qty
    table_df["_recommended_van_load"] = (
        table_df["_units_to_load"].apply(pack_qty)
        + table_df["_opening_stock"].apply(pack_qty)
    )
    table_df = table_df.sort_values("_recommended_van_load", ascending=False)
    table_rows: list[VanLoadTableRow] = []
    for _, r in table_df.iterrows():
        cls_raw = r["_class"]
        cls = (
            str(cls_raw)
            if pd.notna(cls_raw) and str(cls_raw).strip() and str(cls_raw) != "nan"
            else None
        )
        # Minimal explain dict; ExplainabilityModal renders each field verbatim.
        def _opt_bool(v: Any) -> bool | None:
            if v is None:
                return None
            try:
                if isinstance(v, float) and math.isnan(v):
                    return None
            except (TypeError, ValueError):
                pass
            return bool(v)

        explain = {
            "opening_stock": _to_float(r["_opening_stock"]),
            "recent_avg_per_selling_day": _opt_float(r["_recent_avg_per_selling_day"]),
            "expected_demand": _opt_float(r["_expected_demand"]),
            "forecast_below_recent": _opt_bool(r.get("_forecast_below_recent")),
            "guard_skipped": bool(r.get("_guard_skipped", False)),
        }
        # Per-item truck weight = fresh + carried, both ceiled to whole packs.
        units_to_load_int = float(pack_qty(r["_units_to_load"]))
        opening_int = float(pack_qty(r["_opening_stock"]))
        table_rows.append(
            VanLoadTableRow(
                item_code=str(r["_item_code"]),
                item_name=str(r["_item_name"]),
                # Ceil to integer: SKU ships in whole packs (matches RO's get_van_items rounding).
                units_to_load=units_to_load_int,
                # Total truck weight = opening_stock + units_to_load.
                recommended_van_load=units_to_load_int + opening_int,
                p_demand=_opt_float(r["_p_demand"]),
                demand_class=cls,
                lower_bound=_opt_float(r["_lower_bound"]),
                upper_bound=_opt_float(r["_upper_bound"]),
                has_real_confidence=_has_real_confidence(cls),
                explain=explain,
            )
        )

    # Per-(item, date) rows for tile popovers; same shape as PastPerformanceItem.
    # All values read from df (van_load_view); rep side from persisted yaumi_* columns.
    catalog = _load_item_catalog()
    # Vectorise the row materialisation: iterate over ``df.to_dict("records")``
    # so each access is an O(1) dict lookup instead of a pandas Series
    # __getitem__ (which is ~50x slower per access). On the hot drill-down
    # path for a large route this cuts a multi-second response to milliseconds.
    items_payload: list[VanLoadPageViewItem] = []
    needed_cols = (
        "_item_code", "_item_name", "_opening_stock", "_units_to_load",
        "yaumi_opening_stock", "yaumi_fresh_load", "yaumi_total_van_load",
        "yaumi_leftover", "actual_sold", "leftover_to_next_day",
        "forecast_dormant",
    )
    # Ensure every column exists so we can rely on plain dict access below.
    for c in needed_cols:
        if c not in df.columns:
            df[c] = None
    records = df[list(needed_cols)].to_dict("records")
    date_str = str(date)
    for r in records:
        ic = str(r["_item_code"])
        rec_carry = _to_float(r["_opening_stock"])
        rec_fresh = _to_float(r["_units_to_load"])
        rec_van   = rec_carry + rec_fresh
        past_left   = _to_float(r["yaumi_opening_stock"])
        today_alloc = _to_float(r["yaumi_fresh_load"])
        yaumi_total_raw = r["yaumi_total_van_load"]
        rep_van     = _to_float(yaumi_total_raw if yaumi_total_raw is not None
                                else past_left + today_alloc)
        sold_v      = _to_float(r["actual_sold"])
        # actual_lo = yaumi_leftover[d]; rec_lo = leftover_to_next_day[d]. NULL preserved for "no rep data".
        actual_lo   = _to_float(r["yaumi_leftover"])
        rec_lo      = _to_float(r["leftover_to_next_day"])
        cat_name    = str(catalog.get(ic, {}).get("category") or "")
        # _opt_float preserves NULL distinction so UI shows "no rep data" instead of fake zero.
        yaumi_open  = _opt_float(r["yaumi_opening_stock"])
        yaumi_fresh = _opt_float(r["yaumi_fresh_load"])
        yaumi_total = _opt_float(r["yaumi_total_van_load"])
        yaumi_left  = _opt_float(r["yaumi_leftover"])
        # forecast_dormant: CSV surfaces 0/1/NaN; NaN/missing -> None.
        _dormant_raw = r["forecast_dormant"]
        if _dormant_raw is None:
            forecast_dormant_val: bool | None = None
        else:
            try:
                if isinstance(_dormant_raw, float) and math.isnan(_dormant_raw):
                    forecast_dormant_val = None
                else:
                    forecast_dormant_val = bool(int(float(_dormant_raw)))
            except (TypeError, ValueError):
                forecast_dormant_val = None
        if (
            rep_van == 0.0
            and rec_van == 0.0
            and sold_v == 0.0
            and actual_lo == 0.0
            and rec_lo == 0.0
            and yaumi_total is None
        ):
            continue
        items_payload.append(
            VanLoadPageViewItem(
                itemCode=ic,
                itemName=str(r["_item_name"]),
                categoryName=cat_name,
                date=date_str,
                rep_van_load=round(rep_van, 2),
                recommended_van_load=round(rec_van, 2),
                actual_sold=round(sold_v, 2),
                actual_leftover=round(actual_lo, 2),
                recommended_leftover=round(rec_lo, 2),
                yaumi_opening_stock=round(yaumi_open, 2) if yaumi_open is not None else None,
                yaumi_fresh_load=round(yaumi_fresh, 2) if yaumi_fresh is not None else None,
                yaumi_total_van_load=round(yaumi_total, 2) if yaumi_total is not None else None,
                yaumi_leftover=round(yaumi_left, 2) if yaumi_left is not None else None,
                forecast_dormant=forecast_dormant_val,
            )
        )
    # Sort: recommended_leftover desc, recommended_van_load desc, itemCode asc.
    items_payload.sort(
        key=lambda it: (
            -it.recommended_leftover,
            -it.recommended_van_load,
            it.itemCode,
        )
    )

    # Identity: sum(items[*].recommended_van_load) == summary.van_load_qty (drift -> log).
    drift_threshold = float(get_settings().reconciliation_items_drift_threshold)
    items_van_load_sum = round(sum(it.recommended_van_load for it in items_payload), 2)
    if abs(items_van_load_sum - round(summary.van_load_qty, 2)) > drift_threshold:
        logger.warning(
            "van_load_page_view items[].recommended_van_load sum %.2f does not "
            "reconcile with summary.van_load_qty %.2f (drift %.2f) route=%s date=%s",
            items_van_load_sum, summary.van_load_qty,
            items_van_load_sum - summary.van_load_qty, route_code, date,
        )

    return VanLoadPageView(
        success=True,
        available=True,
        route_code=route_code,
        date=date,
        reconciled=have_recon,
        summary=summary,
        chart_top_n=chart_top_n,
        table_rows=table_rows,
        items=items_payload,
    )


# ----------------------------------------------------------------------
# ForecastDrawer (Upcoming plan)
# ----------------------------------------------------------------------


def _today_iso() -> str:
    """Today as YYYY-MM-DD in local TZ; mirrors legacy frontend todayIso()."""
    return pd.Timestamp.now().normalize().strftime("%Y-%m-%d")


@router.get("/forecast-drawer", response_model=ForecastDrawerView)
def forecast_drawer_page_view(
    route_code: str | None = Query(None, description="Route filter"),
    item_codes: list[str] | None = Query(
        None,
        description=(
            "Optional item filter. Repeat for multiple items "
            "(``?item_codes=A&item_codes=B``)."
        ),
    ),
    from_date: str | None = Query(
        None,
        description="YYYY-MM-DD; defaults to today (forward-looking only).",
    ),
    svc: ArtifactService = Depends(get_artifact_service),
) -> ForecastDrawerView:
    """Composite payload for the Upcoming-plan drawer; scope (route, items, date>=from_date).

    Reconciles via engine if DB values absent; band only on single-SKU scope.
    Cross-checks identity total_van_load == sum(chart predicted).
    """
    cutoff = (from_date or _today_iso()).strip()
    items_in = [str(c).strip() for c in (item_codes or []) if str(c).strip()]

    # Same enriched view as van_load_page_view; shared engine run per mtime window.
    fc_df = svc.van_load_view_enriched()
    if fc_df.empty:
        return ForecastDrawerView(
            available=False,
            message="No forecast data available",
            route_code=route_code,
            item_codes=items_in,
            from_date=cutoff,
        )

    pred_col = _detect_predicted_col(fc_df)
    if pred_col is None:
        return ForecastDrawerView(
            available=False,
            message="Forecast frame has no prediction column",
            route_code=route_code,
            item_codes=items_in,
            from_date=cutoff,
        )

    df = fc_df.copy()
    df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce").dt.strftime("%Y-%m-%d")
    if route_code:
        df = df[df["RouteCode"].astype(str) == str(route_code)]
    if items_in:
        df = df[df["ItemCode"].astype(str).isin(items_in)]
    df = df[df["TrxDate"] >= cutoff]

    if df.empty:
        return ForecastDrawerView(
            available=False,
            message="No upcoming forecast in this scope",
            route_code=route_code,
            item_codes=items_in,
            from_date=cutoff,
        )

    have_recon = "recommended_load" in df.columns

    if not have_recon:
        logger.error(
            "forecast_drawer_reconciliation_degraded route=%s items=%s from=%s",
            route_code,
            items_in,
            cutoff,
        )

    if "recommended_load" in df.columns:
        df["_units_to_load"] = (
            pd.to_numeric(df["recommended_load"], errors="coerce").fillna(0.0)
        )
    else:
        df["_units_to_load"] = (
            pd.to_numeric(df[pred_col], errors="coerce").fillna(0.0)
        )

    lb_col = _first_present(df, ("load_lower_bound", "lower_bound", "q_10"))
    ub_col = _first_present(df, ("load_upper_bound", "upper_bound", "q_90"))
    df["_lower_bound"] = (
        pd.to_numeric(df[lb_col], errors="coerce")
        if lb_col
        else pd.Series([None] * len(df), index=df.index, dtype="object")
    )
    df["_upper_bound"] = (
        pd.to_numeric(df[ub_col], errors="coerce")
        if ub_col
        else pd.Series([None] * len(df), index=df.index, dtype="object")
    )

    df["_p_demand"] = pd.to_numeric(df.get("p_demand"), errors="coerce")
    cls_col = _first_present(df, ("class", "demand_class"))
    df["_class"] = (
        df[cls_col].astype(str).str.lower()
        if cls_col
        else pd.Series([""] * len(df), index=df.index)
    )
    df["_item_code"] = df["ItemCode"].astype(str)
    name_col = _first_present(df, ("ItemName", "item_name"))
    df["_item_name"] = (
        df[name_col].astype(str) if name_col else df["_item_code"]
    )

    catalog = _load_item_catalog()
    if catalog:
        name_missing = (
            df["_item_name"].isna()
            | (df["_item_name"].astype(str).str.strip() == "")
            | (df["_item_name"].astype(str).str.lower() == "nan")
        )
        if name_missing.any():
            df.loc[name_missing, "_item_name"] = df.loc[
                name_missing, "_item_code"
            ].map(lambda c: str(catalog.get(c, {}).get("name") or c))

    df["_opening_stock"] = pd.to_numeric(
        df.get("opening_stock", 0.0), errors="coerce"
    ).fillna(0.0)
    # expected_demand + recent_avg drive the "why this size" pair (mirrors van-load explain).
    _recent_avg_col = _first_present(
        df, ("recent_avg_per_selling_day", "RecentAvgPerSellingDay"),
    )
    df["_recent_avg_per_selling_day"] = (
        pd.to_numeric(df[_recent_avg_col], errors="coerce").fillna(0.0)
        if _recent_avg_col else pd.Series([0.0] * len(df), index=df.index)
    )
    _expected_col = _first_present(df, ("expected_demand", "ExpectedDemand"))
    df["_expected_demand"] = (
        pd.to_numeric(df[_expected_col], errors="coerce").fillna(0.0)
        if _expected_col else pd.Series([0.0] * len(df), index=df.index)
    )

    # Drop zero-load rows so chart/table totals stay consistent; guard mask applied upstream.
    df = df[df["_units_to_load"] > 0]
    if df.empty:
        return ForecastDrawerView(
            available=False,
            message="No upcoming load in this scope",
            route_code=route_code,
            item_codes=items_in,
            from_date=cutoff,
        )

    # Quantiles not additive across SKUs; band shown only for single-SKU scope.
    distinct_skus = int(df["_item_code"].nunique())
    show_band = distinct_skus == 1

    # Per-day chart values use the same pack_qty cells as the table so identities hold.
    from common.numeric import pack_qty
    df = df.assign(
        _qty_pack=df["_units_to_load"].apply(pack_qty),
        _lb_pack=df["_lower_bound"].apply(pack_qty),
        _ub_pack=df["_upper_bound"].apply(pack_qty),
    )
    if show_band:
        chart_grouped = (
            df.groupby("TrxDate", as_index=False)
            .agg(
                predicted=("_qty_pack", "sum"),
                q10=("_lb_pack", "sum"),
                q90=("_ub_pack", "sum"),
            )
            .sort_values("TrxDate")
        )
    else:
        chart_grouped = (
            df.groupby("TrxDate", as_index=False)
            .agg(predicted=("_qty_pack", "sum"))
            .sort_values("TrxDate")
        )

    chart_data = [
        ForecastDrawerChartPoint(
            date=str(r["TrxDate"]),
            predicted=float(r["predicted"]),
            q10=float(r["q10"]) if show_band else None,
            q90=float(r["q90"]) if show_band else None,
        )
        for _, r in chart_grouped.iterrows()
    ]

    # ---- Summary tiles -----------------------------------------------
    horizon_days = int(len(chart_data))
    total_van_load = float(sum(p.predicted for p in chart_data))
    avg_per_day = (total_van_load / horizon_days) if horizon_days > 0 else 0.0
    window_start = chart_data[0].date if chart_data else None
    window_end = chart_data[-1].date if chart_data else None

    # ---- Table rows --------------------------------------------------
    table_df = df.sort_values(
        ["TrxDate", "_units_to_load"], ascending=[True, False]
    )
    table_rows: list[ForecastDrawerTableRow] = []
    for _, r in table_df.iterrows():
        cls_raw = r["_class"]
        cls = (
            str(cls_raw)
            if pd.notna(cls_raw) and str(cls_raw).strip() and str(cls_raw) != "nan"
            else None
        )
        # Minimal explain dict; same field set as VanLoad page-view.
        explain = {
            "opening_stock": _to_float(r["_opening_stock"]),
            "recent_avg_per_selling_day": _to_float(r["_recent_avg_per_selling_day"]),
            "expected_demand": _to_float(r["_expected_demand"]),
        }
        table_rows.append(
            ForecastDrawerTableRow(
                date=str(r["TrxDate"]),
                item_code=str(r["_item_code"]),
                item_name=str(r["_item_name"]),
                # Ceil to integer (matches VanLoadTableRow); smallest loadable pack = 1.
                units_to_load=float(pack_qty(r["_units_to_load"])),
                p_demand=_opt_float(r["_p_demand"]),
                demand_class=cls,
                lower_bound=_opt_float(r["_lower_bound"]),
                upper_bound=_opt_float(r["_upper_bound"]),
                has_real_confidence=_has_real_confidence(cls),
                explain=explain,
            )
        )

    # Identity: total_van_load == sum(chart.predicted); flag drift.
    chart_sum = round(sum(p.predicted for p in chart_data), 2)
    if abs(chart_sum - round(total_van_load, 2)) > float(get_settings().reconciliation_items_drift_threshold):
        logger.error(
            "forecast_drawer_total_chart_mismatch route=%s sum=%s total=%s",
            route_code,
            chart_sum,
            total_van_load,
        )

    summary = ForecastDrawerSummary(
        horizon_days=horizon_days,
        total_van_load=round(total_van_load, 1),
        skus=distinct_skus,
        avg_per_day=round(avg_per_day, 1),
        window_start=window_start,
        window_end=window_end,
        line_count=len(table_rows),
    )

    return ForecastDrawerView(
        success=True,
        available=True,
        route_code=route_code,
        item_codes=items_in,
        from_date=cutoff,
        show_band=show_band,
        reconciled=have_recon,
        summary=summary,
        chart_data=chart_data,
        table_rows=table_rows,
    )
