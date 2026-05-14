"""Page-view endpoints.

One HTTP fetch per page state. Every number on a page (tile, chart,
table) is computed inside one request handler from one snapshot of the
source frame, so the surfaces on the same page cannot disagree.

The webapp is a render layer. It never aggregates, sorts, filters, or
substitutes business fields. All such logic lives here -- single source
of truth, byte-for-byte consistent.

ASCII-only: no smart quotes, em-dashes, mathematical symbols, or other
non-ASCII bytes anywhere in this file or in any string returned by it.
"""

from __future__ import annotations

import logging
import math
from typing import Any, Optional

import pandas as pd
from fastapi import APIRouter, Depends, Query

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
from demand_forecasting_pipeline.services.db_pool import get_pool
from demand_forecasting_pipeline.services.reconciliation.enrich import (
    _concentrated_buyers_index,
    _journey_index,
)
from demand_forecasting_pipeline.services.reconciliation.van_load_service import VanLoadService

logger = logging.getLogger(__name__)

_DATE_RE = r"^\d{4}-\d{2}-\d{2}$"


router = APIRouter(prefix="/page-views", tags=["page-views"])

# Demand classes whose p_demand is a real probability. Smooth / erratic
# emit synthetic 0/1 fallbacks that would skew the risky count, so they
# are excluded from at_risk and from has_real_confidence.
REAL_PROBABILITY_CLASSES = frozenset({"intermittent", "lumpy"})


def _guard_masked_items(route_code: str, date: str) -> frozenset[str]:
    """Items on (route, date) where the journey-aware concentration mask
    would zero the load. Used to tag rows in the explainability popup so
    a 0 load reads as "skipped: top buyer not on today's plan" rather
    than an unexplained zero.

    Identical inputs to ``enrich_with_load``'s mask, just inferred at
    read-time so we don't need a new column on yf_demand_forecast.
    Both indices are mtime-cached, so the cost is paid once per CSV
    revision per process.
    """
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


# ----------------------------------------------------------------------
# Helpers (route-local, not shared -- page-view-shaping concerns only)
# ----------------------------------------------------------------------


def _to_float(x: object) -> float:
    """Coerce to float, treating None / NaN / non-numeric as 0.0."""
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(v) or math.isinf(v):
        return 0.0
    return v


def _opt_float(x: object) -> Optional[float]:
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


def _has_real_confidence(cls: Optional[str]) -> bool:
    if not cls:
        return False
    return str(cls).strip().lower() in REAL_PROBABILITY_CLASSES


def _first_present(df: pd.DataFrame, names: tuple[str, ...]) -> Optional[str]:
    for n in names:
        if n in df.columns:
            return n
    return None


# Item catalog (price + name) for revenue + display. Single source of
# truth: ``data/imports/sales_recent.csv``, which data_import's
# /eda/items endpoint also reads from. We keep an in-process mtime-keyed
# cache so consecutive page-view requests don't re-aggregate the file.
_CATALOG_CACHE: dict[str, object] = {"mtime": 0.0, "by_item": {}}


# Cross-source staleness signal. CSV-backed endpoints serve from the
# data_import mirror; if the cascade between the recon cron and
# data_import is lagging, the mirror can be missing rows the DB already
# has. We use a CONTENT-based check (row count on today's date) rather
# than a timestamp comparison because:
#   * SQL Server returns ``GETDATE()`` in the server's local TZ (UTC in
#     this deployment); Windows ``os.stat().st_mtime`` is Unix epoch
#     anchored to the OS's local TZ (Dubai). Naive datetimes from pyodbc
#     carry no TZ, so any time-arithmetic between the two surfaces is
#     ambiguous by exactly the server vs OS offset.
#   * Atomic CSV rewrites bump ``mtime`` even when no rows changed, so
#     ``mtime`` is a "last write" signal, not a "newest data" signal.
#
# Comparing today's row count is robust to both: a missing day-of cron
# cascade leaves the CSV's row count for today below the DB's, which
# the probe catches; cosmetic re-writes leave row counts equal, so no
# false positive.
#
# The probe is mtime-cached: re-query only when the CSV is rewritten.
# Between rewrites the comparison can only widen (DB grows, CSV stays
# put), so a single warning per CSV revision is sufficient.
_STALENESS_CACHE: dict[str, Any] = {
    "mtime": -1.0,
    "csv_today_rows": None,
    "db_today_rows": None,
}


def _log_sales_transactions_staleness(route_code: str, date: str) -> None:
    """Emit a WARNING when the sales_transactions CSV is missing rows
    for ``date`` that exist in the DB.

    Content-based check: compares CSV row count to DB row count for the
    queried date. Robust to:
      * SQL Server TZ vs OS TZ (no time math involved).
      * Cosmetic CSV rewrites (atomic rename bumps mtime even when row
        count is unchanged -- this probe stays silent in that case).

    Mtime-keyed: one DB probe per CSV revision. Between rewrites the
    answer cannot change in the direction that fires (DB only grows;
    CSV stays put), so caching is safe.
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
        # Count CSV rows for the queried date. Streamed read with usecols
        # so we never pull the whole frame into memory -- one column,
        # one filter, one count.
        try:
            csv_today = int(
                pd.read_csv(csv_path, usecols=["TrxDate"], low_memory=False)
                .pipe(lambda d: (d["TrxDate"].astype(str) == str(date)).sum())
            )
        except Exception as exc:
            logger.warning(
                "van_load_staleness_csv_count_failed route=%s date=%s err=%s",
                route_code, date, exc,
            )
            return
        try:
            probe_pool = get_pool(
                s.db.connection_string(),
                max_connections=max(int(s.db.retry_attempts) + 1, 4),
                connect_timeout=int(s.db.connection_timeout),
                query_timeout=int(s.db.query_timeout),
                autocommit=True,
            )
            with probe_pool.acquire() as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM [YaumiAIML].[dbo].[yf_sales_transactions] "
                    "WITH (NOLOCK) WHERE trx_date = ?;",
                    (str(date),),
                )
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

    # Only fire when DB has MORE rows than CSV for this date -- the
    # direction that indicates a failed cascade. Equal counts (the
    # healthy case) and CSV-has-more (impossible under correct flow,
    # but treated as benign) stay silent.
    if db_today > csv_today:
        logger.warning(
            "sales_transactions_csv_missing_rows route=%s date=%s "
            "csv_rows=%d db_rows=%d gap=%d",
            route_code, date, csv_today, db_today, db_today - csv_today,
        )


def _load_item_catalog() -> dict[str, dict[str, object]]:
    """Return ``{ItemCode: {"name": str, "price": float | None}}``.

    Reads from ``shared_data_dir/sales_recent_file`` (the same CSV
    data_import's catalog endpoint reads from). Cached by file mtime so
    a hot file is read at most once per refresh.
    """
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

    # Mean unit price per item over the available window. Mirrors the
    # avg_price field /eda/items emits. Last name wins when an item has
    # been renamed (rare in this dataset; deterministic via groupby).
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
    """Composite payload for the VanLoad route-detail page.

    Pipeline (all inside one request, one frame snapshot):
      1. Read the unified van-load forecast frame (Forecast + Test).
      2. Scope to (route_code, date).
      3. Reconcile via the V5_b engine if DB-stored values are absent.
         Same canonical function ``db_pusher`` and the daily 03:30 cron
         use, so the inline value equals what the next cron will write.
      4. Compute the carry-aware summary (van_load = carried + issued).
      5. Slice top-N for the chart, sorted desc by units_to_load.
      6. Emit table rows, sorted desc, with reconciled bounds and the
         server-side has_real_confidence verdict.
      7. Cross-check the identity carried + issued == van_load_qty.
    """
    # Surface CSV-vs-DB staleness via WARNING log. No fallback -- the
    # CSV is still the served source; this is purely an ops signal so
    # a lagging cascade is visible rather than silent.
    _log_sales_transactions_staleness(str(route_code), str(date))

    # ``van_load_view_enriched`` returns the reconciled van-load frame --
    # DB-stored when the cron has filled ``recommended_load`` for the
    # window, else lazily computed via the canonical engine and cached
    # behind a multi-file mtime key. Centralised so the engine runs at
    # most once per (forecast, sales_transactions, sales_recent,
    # customer_data, journey_plan) snapshot fleet-wide.
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

    # Forecast frame's own price column wins when present; otherwise
    # fall back to the shared sales-recent catalog. Same source the
    # /eda/items endpoint serves so prices match across the stack.
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
        # Fill missing prices and names from the catalog. Rows that
        # already carry a value keep it -- the forecast frame is the
        # authoritative source when both have a value.
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

    # ``expected_demand`` is the engine's final per-day target (post bias
    # correction + recent-pattern envelope); ``recent_avg_per_selling_day``
    # is the historical anchor for context. Together they tell the
    # supervisor *why* this size. CSV mirror exposes either the
    # PascalCase (DB-canonical) or the snake_case (post-FileStorage
    # rename) form; both accepted.
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


    # ---- Summary tile (carry-aware) -----------------------------------
    carry_mask = (df["_units_to_load"] > 0) | (df["_opening_stock"] > 0)
    carry_df = df.loc[carry_mask]

    carried_qty = float(
        carry_df.loc[carry_df["_opening_stock"] > 0, "_opening_stock"].sum()
    )
    issued_qty = float(
        carry_df.loc[carry_df["_units_to_load"] > 0, "_units_to_load"].sum()
    )
    carried_items = int(
        carry_df.loc[carry_df["_opening_stock"] > 0, "_item_code"].nunique()
    )
    issued_items = int(
        carry_df.loc[carry_df["_units_to_load"] > 0, "_item_code"].nunique()
    )
    van_load_items = int(carry_df["_item_code"].nunique())
    van_load_qty = carried_qty + issued_qty

    # Revenue + at-risk: load-this set only (units_to_load > 0).
    load_df = df.loc[df["_units_to_load"] > 0]
    has_revenue = bool(load_df["_price"].notna().any())
    revenue_val = (
        float((load_df["_units_to_load"] * load_df["_price"].fillna(0.0)).sum())
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

    # Wire precision matches /predictions/forecast/route-summary (1 dp
    # on quantities, 2 dp on revenue) so the page-view tile is byte-
    # for-byte identical to the route-grid tile a click earlier.
    summary = VanLoadSummaryView(
        van_load_qty=round(van_load_qty, 1),
        van_load_items=van_load_items,
        carried_qty=round(carried_qty, 1),
        carried_items=carried_items,
        issued_qty=round(issued_qty, 1),
        issued_items=issued_items,
        revenue=round(revenue_val, 2) if revenue_val is not None else None,
        has_revenue=has_revenue,
        at_risk=at_risk,
    )

    # Cross-check the summary identity. Floating-point sum should equal
    # the components within 1e-6 by construction; a larger delta means
    # the masks drifted apart and the caller should be alerted.
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
            # Same integer-ceil contract as the table row so chart and
            # table always show identical numbers per SKU.
            predicted=float(math.ceil(_to_float(r["_units_to_load"]))),
        )
        for _, r in chart_df.iterrows()
    ]

    # ---- Table --------------------------------------------------------
    # Carry-aware: keep rows with ANY truck contribution -- fresh load,
    # yesterday's leftover, or both. Pure-carry rows (opening > 0,
    # fresh = 0) are real physical inventory the rep is managing today
    # and must surface in the table so the tile total (= sum of
    # recommended_van_load) reconciles with the table by construction.
    # Guard-masked rows stay in so the explainability popup can reach
    # them and explain the zero. Chart stays load-only (zero-load bars
    # would render as gaps anyway). Sort by total truck weight so
    # heavy items lead, regardless of which side (fresh vs carry)
    # produced the weight.
    table_df = df.loc[
        (df["_units_to_load"] > 0)
        | (df["_opening_stock"] > 0)
        | df["_guard_skipped"].astype(bool)
    ].copy()
    table_df["_recommended_van_load"] = (
        table_df["_units_to_load"].apply(lambda x: math.ceil(_to_float(x)))
        + table_df["_opening_stock"].apply(lambda x: math.ceil(_to_float(x)))
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
        # Minimal explain dict -- every field has a direct render in
        # ExplainabilityModal. Three sections:
        #   "How we got the load":
        #     opening_stock (carry) + fresh-load -> total truck weight.
        #     expected_demand (engine target) + recent_avg (anchor) as
        #     the "why this size" pair.
        #   forecast_below_recent: warning banner driven server-side.
        #   guard_skipped: banner for journey-mask zeroed rows.
        # All values are stored columns on yf_sales_transactions, surfaced
        # here verbatim -- no client-side derivation.
        def _opt_bool(v: Any) -> Optional[bool]:
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
        # Per-item truck weight = fresh load (engine recommendation) +
        # carried stock (yesterday's leftover, forward-filled). Both
        # ceil to whole packs so the headline equals the sum of its
        # parts without rounding drift.
        units_to_load_int = float(math.ceil(_to_float(r["_units_to_load"])))
        opening_int = float(math.ceil(_to_float(r["_opening_stock"])))
        table_rows.append(
            VanLoadTableRow(
                item_code=str(r["_item_code"]),
                item_name=str(r["_item_name"]),
                # Ceil to integer: matches the truck-load reality (a SKU
                # ships in whole packs) and the RO data-manager's
                # ``get_van_items`` rounding. Also kills the cosmetic
                # "0.0" display that 1dp rounding produced for sub-half
                # predictions (0 < x < 0.05) -- those rows now render as
                # 1, the smallest pack the rep would actually load.
                units_to_load=units_to_load_int,
                # Total truck weight for THIS item. The modal headline
                # reads this; the math chain explains it as
                # ``opening_stock + units_to_load``.
                recommended_van_load=units_to_load_int + opening_int,
                p_demand=_opt_float(r["_p_demand"]),
                demand_class=cls,
                lower_bound=_opt_float(r["_lower_bound"]),
                upper_bound=_opt_float(r["_upper_bound"]),
                has_real_confidence=_has_real_confidence(cls),
                explain=explain,
            )
        )

    # ---- Per-(item, date) rows for the headline tile's popovers ------
    # Same shape as PastPerformanceItem so a frontend renderer can share
    # the row component between the past-performance drawer and today's
    # van-load page. ``date`` is the queried date for every row.
    #
    # Single source: every per-item value below is read from ``df`` (=
    # van_load_view, which LEFT JOINs sales_transactions.csv onto the
    # forecast frame). Rep side comes from the persisted ``yaumi_*``
    # columns -- same cells the daily cron writes from VW_GET_CLOSING_STOCK
    # and VW_GET_LOAD_ALLOCATION_DETAILS, so the tile, the popover,
    # and the past-performance drawer always agree.
    catalog = _load_item_catalog()
    items_payload: list[VanLoadPageViewItem] = []
    for _, r in df.iterrows():
        ic = str(r["_item_code"])
        rec_carry = _to_float(r["_opening_stock"])
        rec_fresh = _to_float(r["_units_to_load"])
        rec_van   = rec_carry + rec_fresh
        past_left   = _to_float(r.get("yaumi_opening_stock"))
        today_alloc = _to_float(r.get("yaumi_fresh_load"))
        rep_van     = _to_float(r.get("yaumi_total_van_load", past_left + today_alloc))
        sold_v      = _to_float(r.get("actual_sold"))
        # Canonical sources: yaumi_leftover[d] for actual; leftover_to_next_day[d] for recommended.
        # NULL stays NULL so the UI can render "no rep data" honestly.
        actual_lo   = _to_float(r.get("yaumi_leftover"))
        rec_lo      = _to_float(r.get("leftover_to_next_day"))
        cat_name    = str(catalog.get(ic, {}).get("category") or "")
        # Rep's actual loading from yf_sales_transactions.yaumi_*. The
        # row carries NULL when reconciliation_refresh found no rep
        # activity (no closing, no allocation) for the (route, item,
        # date). ``_opt_float`` preserves that distinction so the UI
        # can render "no rep data" instead of a fake zero.
        yaumi_open  = _opt_float(r.get("yaumi_opening_stock"))
        yaumi_fresh = _opt_float(r.get("yaumi_fresh_load"))
        yaumi_total = _opt_float(r.get("yaumi_total_van_load"))
        yaumi_left  = _opt_float(r.get("yaumi_leftover"))
        # Dormancy flag from yf_sales_transactions.forecast_dormant.
        # Defensive parse: the CSV mirror surfaces it as 0/1/NaN (DB
        # roundtrip via CAST AS INT), and pre-backfill rows are NULL.
        # NaN / missing -> None (no diagnostic yet populated), otherwise
        # cast to bool through int so "0" / "1" string forms also work.
        _dormant_raw = r.get("forecast_dormant")
        if _dormant_raw is None:
            forecast_dormant_val: Optional[bool] = None
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
                date=str(date),
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
    # Sort: recommended_leftover desc (items still left over under our
    # policy surface first), then recommended_van_load desc, itemCode asc.
    items_payload.sort(
        key=lambda it: (
            -it.recommended_leftover,
            -it.recommended_van_load,
            it.itemCode,
        )
    )

    # Identity log: sum(items[*].recommended_van_load) on today's rows
    # equals today's headline van_load_qty. Both walk the same fc_df
    # rows so the only way they diverge is a row included in one and
    # filtered from the other. Tolerance lifted from settings.
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
    """Today as YYYY-MM-DD in the local timezone of the service host.

    Mirrors the legacy frontend ``todayIso()`` helper -- both round to
    midnight local. The drawer is strictly forward-looking so the
    cutoff matters when the cron has produced a same-day forecast that
    the supervisor has already acted on.
    """
    return pd.Timestamp.now().normalize().strftime("%Y-%m-%d")


@router.get("/forecast-drawer", response_model=ForecastDrawerView)
def forecast_drawer_page_view(
    route_code: Optional[str] = Query(None, description="Route filter"),
    item_codes: Optional[list[str]] = Query(
        None,
        description=(
            "Optional item filter. Repeat for multiple items "
            "(``?item_codes=A&item_codes=B``)."
        ),
    ),
    from_date: Optional[str] = Query(
        None,
        description="YYYY-MM-DD; defaults to today (forward-looking only).",
    ),
    svc: ArtifactService = Depends(get_artifact_service),
) -> ForecastDrawerView:
    """Composite payload for the Upcoming-plan drawer.

    Pipeline:
      1. Read the unified van-load forecast frame.
      2. Scope to (route_code, optional item_codes, date >= from_date).
      3. Reconcile via the engine if DB-stored values are absent.
      4. Aggregate per-day predicted/q10/q90 (band only on single-SKU).
      5. Emit table rows sorted (date asc, units_to_load desc).
      6. Cross-check identity: total_van_load == sum(chart predicted).
    """
    cutoff = (from_date or _today_iso()).strip()
    items_in = [str(c).strip() for c in (item_codes or []) if str(c).strip()]

    # Same source-of-truth read as ``van_load_page_view`` -- the service
    # method handles the DB-stored vs engine-fallback decision and caches
    # the enriched frame so both endpoints share a single engine run per
    # mtime window.
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
    # ``expected_demand`` is the engine's final per-day target;
    # ``recent_avg_per_selling_day`` is the historical anchor for the
    # "why this size" pair on the modal. Mirrors the van-load explain
    # dict so the same modal renders consistently on both surfaces.
    # Forward-only rows skip the below-recent banner -- there's no past
    # pattern to compare to yet.
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

    # The drawer drops zero-load rows from the table because they would
    # be misleading to act on; the chart band derives from the same set
    # so totals across surfaces remain consistent. Journey-aware mask
    # is already applied upstream in the cron, so guarded rows have
    # _units_to_load == 0 and never make it past this filter.
    df = df[df["_units_to_load"] > 0]
    if df.empty:
        return ForecastDrawerView(
            available=False,
            message="No upcoming load in this scope",
            route_code=route_code,
            item_codes=items_in,
            from_date=cutoff,
        )

    # Quantiles are not additive across SKUs. The band is meaningful only
    # when the drawer is scoped to a single SKU.
    distinct_skus = int(df["_item_code"].nunique())
    show_band = distinct_skus == 1

    # ---- Per-day chart series ----------------------------------------
    if show_band:
        chart_grouped = (
            df.groupby("TrxDate", as_index=False)
            .agg(
                predicted=("_units_to_load", "sum"),
                q10=("_lower_bound", "sum"),
                q90=("_upper_bound", "sum"),
            )
            .sort_values("TrxDate")
        )
    else:
        chart_grouped = (
            df.groupby("TrxDate", as_index=False)
            .agg(predicted=("_units_to_load", "sum"))
            .sort_values("TrxDate")
        )

    chart_data = [
        ForecastDrawerChartPoint(
            date=str(r["TrxDate"]),
            predicted=round(_to_float(r["predicted"]), 2),
            q10=round(_to_float(r["q10"]), 2) if show_band else None,
            q90=round(_to_float(r["q90"]), 2) if show_band else None,
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
        # Minimal explain dict -- same field set as the VanLoad page-view's
        # explain so the modal renders identically on both surfaces.
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
                # Ceil to integer (same contract as VanLoadTableRow):
                # 0 < x < 0.05 rows previously rendered as 0.0 due to
                # 1dp rounding; ceil emits 1, the smallest pack the rep
                # actually loads.
                units_to_load=float(math.ceil(_to_float(r["_units_to_load"]))),
                p_demand=_opt_float(r["_p_demand"]),
                demand_class=cls,
                lower_bound=_opt_float(r["_lower_bound"]),
                upper_bound=_opt_float(r["_upper_bound"]),
                has_real_confidence=_has_real_confidence(cls),
                explain=explain,
            )
        )

    # Cross-check identity: total_van_load equals the chart series sum
    # by construction; flag if rounding pushed them apart.
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
