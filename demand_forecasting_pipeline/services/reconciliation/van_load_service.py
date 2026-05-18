"""Assemble the actual van composition for one (route, date), and emit
per-day aggregates for the past-performance chart.

For yesterday-and-earlier the data comes from the shared CSVs that
``data_import`` refreshes nightly. For today (live) the same shape is
fetched from data_import's ``/eda/live-van-composition`` endpoint via
HTTP -- one round-trip, 60-s-cached at the data_import side.

Every per-item record has the same shape regardless of source:

    {
        item_code, item_name, category_code, category_name,
        past_leftover, today_allocation, van_load,
        sold_qty, bad_return_qty, good_return_qty,
        leftover_now, end_closing,
    }
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from datetime import date as _date_cls, datetime
from pathlib import Path
from typing import Any, Optional

import httpx
import pandas as pd

from demand_forecasting_pipeline.config.settings import Settings, get_settings
from demand_forecasting_pipeline.observability import LIVE_FETCH_TIMEOUTS

logger = logging.getLogger(__name__)

# Field names live in one place so callers and tests reference the same keys.
ITEM_FIELDS: tuple[str, ...] = (
    "item_code", "item_name", "category_code", "category_name",
    "past_leftover", "today_allocation", "van_load",
    "sold_qty", "bad_return_qty", "good_return_qty",
    "leftover_now", "end_closing",
)

class VanLoadService:
    """Single shape for van composition. CSVs for historical, HTTP for live."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        # Cache sizes/TTLs come from Settings so an ops change doesn't
        # require a redeploy. Cached on the instance so a reconfigured
        # service after restart picks the new values up immediately.
        self._max_cache_entries: int = int(self._s.van_load_max_cache_entries)
        self._csv_cache_ttl_seconds: int = int(self._s.van_load_csv_cache_ttl_seconds)
        self._live_cache_ttl_seconds: int = int(self._s.van_load_live_cache_ttl_seconds)
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._csv_lock = threading.Lock()
        self._csv_cache: dict[Path, tuple[tuple[int, int], pd.DataFrame]] = {}

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def get(self, route_code: str, date: str) -> dict[str, Any]:
        date_n = self._normalise_date(date)
        ttl = (self._live_cache_ttl_seconds if self._is_today_or_future(date_n)
               else self._csv_cache_ttl_seconds)
        return self._cached(
            f"{route_code}::{date_n}",
            lambda: self._fetch(route_code, date_n),
            ttl_seconds=ttl,
        )

    def past_performance(
        self,
        route_code: str,
        start_date: str,
        end_date: str,
    ) -> dict[str, Any]:
        """Active-day axis for the past-performance chart.

        Returns every day in ``[start_date, end_date]`` (inclusive) that
        had activity for this route -- allocation / sales / returns. A
        day with zero activity is silently dropped from the daily array
        because the downstream chart has nothing to plot for it; the
        ``start_date`` / ``end_date`` echo lets the client confirm the
        requested window.
        """
        rcode = str(route_code)
        try:
            start = pd.Timestamp(start_date).normalize()
            end   = pd.Timestamp(end_date).normalize()
        except (ValueError, TypeError):
            return {"route_code": rcode, "start_date": start_date, "end_date": end_date,
                    "active_days": 0, "daily": []}
        if start > end:
            return {"route_code": rcode, "start_date": start_date, "end_date": end_date,
                    "active_days": 0, "daily": []}

        alloc   = self._load_csv(self._s.load_allocation_file)
        sales   = self._load_csv(self._s.sales_recent_file)
        returns = self._load_csv(self._s.returns_recent_file)

        def _route_dates(df: pd.DataFrame) -> set[pd.Timestamp]:
            if df.empty or "TrxDate" not in df.columns:
                return set()
            return set(df[df.RouteCode.astype(str) == rcode].TrxDate.unique())

        all_active = sorted(_route_dates(alloc) | _route_dates(sales) | _route_dates(returns))
        active = [d for d in all_active if start <= d <= end]

        rows = [{"date": d.strftime("%Y-%m-%d")} for d in active]

        return {
            "route_code":  rcode,
            "start_date":  start_date,
            "end_date":    end_date,
            "active_days": len(rows),
            "daily":       rows,
        }

    def typical_allocation(
        self,
        route_code: str,
        as_of: str,
        lookback_days: int,
    ) -> dict[str, float]:
        """Per-item rolling avg of today's allocation over the last
        ``lookback_days`` *active* days ending at as_of - 1. Active = days
        with any allocation row. Used for vs-typical decision labels."""
        alloc = self._load_csv(self._s.load_allocation_file)
        if alloc.empty:
            return {}
        target = pd.Timestamp(as_of).normalize()
        start  = target - pd.Timedelta(days=lookback_days)
        sub = alloc[
            (alloc.RouteCode.astype(str) == str(route_code))
            & (alloc.TrxDate >= start) & (alloc.TrxDate < target)
        ]
        if sub.empty:
            return {}
        daily = sub.groupby(["ItemCode", "TrxDate"], as_index=False).AllocatedPC.sum()
        avg = daily.groupby("ItemCode").AllocatedPC.mean()
        return {str(i): float(v) for i, v in avg.items()}

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()
        with self._csv_lock:
            self._csv_cache.clear()

    # ------------------------------------------------------------------
    # Source dispatch
    # ------------------------------------------------------------------

    def _fetch(self, route_code: str, date: str) -> dict[str, Any]:
        # Try live first for today (or a forward-looking query). If the
        # data_import endpoint isn't reachable, fall back to CSV so the
        # service never goes blank on a transient network issue.
        if self._is_today_or_future(date) and self._s.data_import_configured:
            live = self._fetch_live(route_code, date)
            if live.get("available"):
                live["source"] = "live"
                return live
            logger.warning("VanLoadService: live fetch failed, falling back to CSV: %s",
                           live.get("message"))
        # ``source`` is always set so the API surface can show whether the
        # response reflects live data_import results or a stale CSV mirror.
        out = self._fetch_from_csv(route_code, date)
        out.setdefault("source", "csv_fallback")
        return out

    def _fetch_live(self, route_code: str, date: str) -> dict[str, Any]:
        url = (f"{self._s.data_import_url.rstrip('/')}"
               f"/api/v1/data/eda/live-van-composition")
        start = time.perf_counter()
        try:
            with httpx.Client(timeout=self._s.http_request_timeout_seconds) as client:
                resp = client.get(url, params={"route_code": route_code, "date": date})
                resp.raise_for_status()
                return resp.json()
        except httpx.TimeoutException as exc:
            latency_ms = round((time.perf_counter() - start) * 1000.0, 1)
            LIVE_FETCH_TIMEOUTS.inc()
            logger.warning(
                "live_van_composition_timeout",
                extra={
                    "route_code": route_code,
                    "date": date,
                    "latency_ms": latency_ms,
                    "timeout_seconds": self._s.http_request_timeout_seconds,
                    "error": repr(exc),
                },
            )
            return {"available": False, "message": f"live fetch timeout: {exc}",
                    "items": [], "totals": {}, "route_code": route_code, "date": date}
        except Exception as exc:
            latency_ms = round((time.perf_counter() - start) * 1000.0, 1)
            logger.warning(
                "live_van_composition_error",
                extra={
                    "route_code": route_code,
                    "date": date,
                    "latency_ms": latency_ms,
                    "error": repr(exc),
                },
            )
            return {"available": False, "message": f"live fetch error: {exc}",
                    "items": [], "totals": {}, "route_code": route_code, "date": date}

    def _fetch_from_csv(self, route_code: str, date: str) -> dict[str, Any]:
        target = pd.Timestamp(date).normalize()
        prior  = target - pd.Timedelta(days=1)
        rcode  = str(route_code)

        closing = self._load_csv(self._s.closing_stock_file)
        alloc   = self._load_csv(self._s.load_allocation_file)
        sales   = self._load_csv(self._s.sales_recent_file)
        returns = self._load_csv(self._s.returns_recent_file)

        # Apply the same forward-fill the DB-write path uses, so a
        # non-trip day (Friday weekend, holiday, rep off) carries the
        # last working day's closing forward into ``past_leftover``
        # instead of dropping the row and reporting an empty truck.
        # Reusing ``forward_fill_closing`` keeps the carry rule in one
        # place -- this is the same primitive ``_fetch_yaumi_loading``
        # calls when populating ``yaumi_opening_stock`` in
        # yf_sales_transactions, so CSV-fallback and DB-backed views
        # report identical rep carry chains. Filtered to the target
        # route up front so the per-(route, item) reindex stays
        # bounded by the active item set for this request.
        if not closing.empty:
            from demand_forecasting_pipeline.services.reconciliation.enrich import (
                forward_fill_closing,
            )
            route_closing = closing[closing.RouteCode.astype(str) == rcode]
            if not route_closing.empty:
                closing = forward_fill_closing(
                    route_closing,
                    int(self._s.opening_stock_lookback_days),
                )

        items: dict[str, dict[str, Any]] = {}

        def _ensure(item_code, name="", cat="", cat_name=""):
            ic = str(item_code or "").strip()
            entry = items.setdefault(ic, {f: 0.0 for f in ITEM_FIELDS})
            entry["item_code"] = ic
            if name and not entry["item_name"]:
                entry["item_name"] = str(name).strip()
            if cat and not entry["category_code"]:
                entry["category_code"] = str(cat).strip()
            if cat_name and not entry["category_name"]:
                entry["category_name"] = str(cat_name).strip()
            return entry

        if not closing.empty:
            prev = closing[(closing.RouteCode.astype(str) == rcode)
                           & (closing.TrxDate == prior)]
            for _, r in prev.iterrows():
                e = _ensure(r.ItemCode, r.get("ItemName"),
                            r.get("CategoryCode"), r.get("CategoryName"))
                e["past_leftover"] = float(r.ClosingQty or 0.0)

            today_close = closing[(closing.RouteCode.astype(str) == rcode)
                                  & (closing.TrxDate == target)]
            for _, r in today_close.iterrows():
                e = _ensure(r.ItemCode, r.get("ItemName"))
                e["end_closing"] = float(r.ClosingQty or 0.0)

        if not alloc.empty:
            sub = alloc[(alloc.RouteCode.astype(str) == rcode)
                        & (alloc.TrxDate == target)]
            for _, r in sub.iterrows():
                e = _ensure(r.ItemCode, r.get("ItemName"),
                            r.get("CategoryCode"), r.get("CategoryName"))
                e["today_allocation"] = float(r.AllocatedPC or 0.0)

        if not sales.empty:
            sub = sales[(sales.RouteCode.astype(str) == rcode)
                        & (sales.TrxDate == target)]
            for _, r in sub.iterrows():
                e = _ensure(r.ItemCode, r.get("ItemName"),
                            None, r.get("CategoryName"))
                e["sold_qty"] = float(r.TotalQuantity or 0.0)

        if not returns.empty:
            sub = returns[(returns.RouteCode.astype(str) == rcode)
                          & (returns.TrxDate == target)]
            for _, r in sub.iterrows():
                e = _ensure(r.ItemCode, r.get("ItemName"),
                            r.get("CategoryCode"), r.get("CategoryName"))
                e["bad_return_qty"]  = float(r.BadReturnQty or 0.0)
                e["good_return_qty"] = float(r.GoodReturnQty or 0.0)

        for e in items.values():
            e["van_load"] = e["past_leftover"] + e["today_allocation"]
            consumed = e["sold_qty"] + e["bad_return_qty"] + e["good_return_qty"]
            e["leftover_now"] = max(0.0, e["van_load"] - consumed)

        items_list = [e for e in items.values()
                      if e["van_load"] > 0 or e["sold_qty"] > 0
                      or e["bad_return_qty"] > 0 or e["good_return_qty"] > 0]
        items_list.sort(key=lambda x: x["van_load"], reverse=True)

        totals = self._totals(items_list)
        return {
            "available": True,
            "source": "csv",
            "route_code": rcode,
            "date": date,
            "items": items_list,
            "totals": totals,
            "fetched_at": pd.Timestamp.now().isoformat(),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _totals(items_list: list[dict[str, Any]]) -> dict[str, Any]:
        # Per-item ``bad_return_qty`` / ``good_return_qty`` stay on each
        # row because ``leftover_now`` depends on them, but no UI consumes
        # the totals -- they're not surfaced here.
        out = {
            "items_count":            len(items_list),
            "past_leftover_total":    0.0,
            "today_allocation_total": 0.0,
            "van_load_total":         0.0,
            "sold_total":             0.0,
            "leftover_now_total":     0.0,
            "items_sold_out":         0,
        }
        for e in items_list:
            out["past_leftover_total"]    += e["past_leftover"]
            out["today_allocation_total"] += e["today_allocation"]
            out["van_load_total"]         += e["van_load"]
            out["sold_total"]             += e["sold_qty"]
            out["leftover_now_total"]     += e["leftover_now"]
            if e["van_load"] > 0 and e["leftover_now"] == 0:
                out["items_sold_out"] += 1
        return out

    def _load_csv(self, filename: str) -> pd.DataFrame:
        path = self._s.shared_data_path(filename)
        if not path.exists():
            return pd.DataFrame()
        # Stat-and-read both happen INSIDE ``_csv_lock``. The previous
        # ordering (stat outside, read outside, write inside) had a
        # classic TOCTOU window: data_import flips the CSV between the
        # stat and the read, the reader gets the new bytes but caches
        # them under the old stat key, and every later reader that
        # observes a NEWER stat would re-read again. Worse, two
        # concurrent readers could both do the (slow) parse on a cache
        # miss. Holding the lock across the parse serialises miss-path
        # readers; cache hits remain cheap (only the stat + dict lookup
        # are taken under the lock).
        with self._csv_lock:
            stat = path.stat()
            key = (stat.st_mtime_ns, stat.st_size)
            cached = self._csv_cache.get(path)
            if cached and cached[0] == key:
                return cached[1]

            try:
                df = pd.read_csv(path, low_memory=False)
                if "TrxDate" in df.columns:
                    df["TrxDate"] = pd.to_datetime(df["TrxDate"], errors="coerce").dt.normalize()
            except Exception as exc:
                logger.error("VanLoadService: failed to read %s: %s", path, exc)
                return pd.DataFrame()

            self._csv_cache[path] = (key, df)
            return df

    @staticmethod
    def _normalise_date(date: str) -> str:
        return pd.Timestamp(date).strftime("%Y-%m-%d")

    @staticmethod
    def _is_today_or_future(date: str) -> bool:
        try:
            d = datetime.strptime(date, "%Y-%m-%d").date()
        except Exception:
            return False
        return d >= _date_cls.today()

    def _cached(self, key: str, loader, *, ttl_seconds: int) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            entry = self._cache.get(key)
            if entry and (now - entry[0]) < ttl_seconds:
                self._cache.move_to_end(key)
                return entry[1]
        value = loader()
        with self._lock:
            self._cache[key] = (now, value)
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_cache_entries:
                self._cache.popitem(last=False)
        return value
