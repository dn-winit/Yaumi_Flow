"""Session lifecycle: initialize -> visit (auto-persisted) + live unplanned poll.

Each process_visit upserts route/customer/items into YaumiAIML as a BackgroundTask;
no separate save step.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from sales_supervision.api.dependencies import (
    get_auto_visit_service,
    get_db_saver,
    get_live_actuals,
    get_session_manager,
)
from sales_supervision.api.schemas import (
    AlsoBoughtRow,
    InitSessionRequest,
    ProcessVisitRequest,
    RedistributionView,
    SavedVisitsResponse,
    SessionResponse,
    SessionSummary,
    UnplannedVisitsResponse,
    VisitResponse,
    VisitResultPayload,
    VisitScore,
)
from sales_supervision.core.constants import TIER_UNPLANNED
from sales_supervision.core.redistribution import (
    compute_buffer_ledger,
    compute_redistribution_for_unplanned,
    shape_redistribution_view,
)
from sales_supervision.core.session import SessionManager
from sales_supervision.models.schemas import SessionItem
from sales_supervision.services.db_saver import DbSaver
from sales_supervision.services.live_actuals import LiveActualsClient

router = APIRouter(prefix="/session", tags=["session"])


# In-memory session registry; LRU + TTL bounds long-running memory.
from sales_supervision.config.settings import get_settings as _get_ss_settings


class _SessionRegistry:
    """LRU + TTL store for in-flight sessions; vends per-session locks for /visit serialisation."""

    def __init__(self, maxsize: int, ttl_seconds: int) -> None:
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._items: OrderedDict[str, tuple[float, object]] = OrderedDict()
        # Locks track session lifetime; outer mutex serialises dict allocate/pop.
        self._locks: dict[str, threading.Lock] = {}
        self._locks_mutex = threading.Lock()

    def _sweep(self) -> None:
        cutoff = time.time() - self._ttl
        stale = [sid for sid, (ts, _) in self._items.items() if ts < cutoff]
        for sid in stale:
            self._items.pop(sid, None)
            self._drop_lock(sid)

    def _drop_lock(self, session_id: str) -> None:
        with self._locks_mutex:
            self._locks.pop(session_id, None)

    def lock_for(self, session_id: str) -> threading.Lock:
        with self._locks_mutex:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[session_id] = lock
            return lock

    def set(self, session_id: str, session: object) -> None:
        self._sweep()
        self._items[session_id] = (time.time(), session)
        self._items.move_to_end(session_id)
        while len(self._items) > self._maxsize:
            evicted, _ = self._items.popitem(last=False)
            self._drop_lock(evicted)

    def get(self, session_id: str):
        self._sweep()
        entry = self._items.get(session_id)
        if entry is None:
            return None
        ts, session = entry
        # Refresh access time; long-lived sessions stay warm.
        self._items[session_id] = (time.time(), session)
        self._items.move_to_end(session_id)
        return session

    def pop(self, session_id: str) -> None:
        self._items.pop(session_id, None)
        self._drop_lock(session_id)


# Lazy singleton: don't capture session_registry_max / session_ttl_seconds at
# import time -- env overrides applied AFTER first import would otherwise be
# silently ignored. ``get_settings()`` is ``@lru_cache``-d so this resolves to
# the same instance every call after first construction.
_REGISTRY_LOCK = threading.Lock()
_sessions_singleton: _SessionRegistry | None = None


def _sessions_registry() -> _SessionRegistry:
    global _sessions_singleton
    if _sessions_singleton is not None:
        return _sessions_singleton
    with _REGISTRY_LOCK:
        if _sessions_singleton is None:
            s = _get_ss_settings()
            _sessions_singleton = _SessionRegistry(
                s.session_registry_max, s.session_ttl_seconds,
            )
    return _sessions_singleton


class _SessionsProxy:
    """Attribute-forwarding shim so existing ``_sessions.foo`` call sites keep
    working without per-call refactors. Resolves the underlying lazy registry
    on every access."""

    def __getattr__(self, name: str) -> Any:
        return getattr(_sessions_registry(), name)


_sessions: Any = _SessionsProxy()


@router.post("/initialize", response_model=SessionResponse)
def initialize_session(
    req: InitSessionRequest,
    background_tasks: BackgroundTasks,
    mgr: SessionManager = Depends(get_session_manager),
    db_saver: DbSaver = Depends(get_db_saver),
    auto_visit_svc=Depends(get_auto_visit_service),
):
    # Page-open is read-only; reconciliation fires as a BackgroundTask. Invalidate
    # the cron's cached session so its next tick picks up today's journey plan.
    if auto_visit_svc is not None and db_saver.available:
        auto_visit_svc.invalidate_route(req.route_code, req.date)
        background_tasks.add_task(
            auto_visit_svc.reconcile_route,
            req.route_code, req.date,
        )

    # Client-supplied recs win (freshest after manual regenerate); server fallback keeps the wire contract optional.
    recs = req.recommendations
    if not recs and auto_visit_svc is not None:
        recs = auto_visit_svc._recs.get_recommendations(req.route_code, req.date) or []
    session = mgr.create_session(req.route_code, req.date, recs)
    saved: dict[str, Any] | None = None
    if db_saver.available:
        # Hydration only consumes actualSales + score; redistributions are loaded on drill-in.
        saved = db_saver.load_session_visits(
            req.route_code, req.date,
            include_redistributions=False,
        )
        if saved:
            mgr.hydrate_saved_visits(session, saved)
    _sessions.set(session.session_id, session)
    # Pin DB-derived visit_totals so /initialize and /session/saved emit byte-identical numbers.
    summary_dict = session.summary()
    if saved and saved.get("visit_totals"):
        summary_dict["visit_totals"] = saved["visit_totals"]
    return SessionResponse(
        success=True,
        session=SessionSummary(**summary_dict),
    )


@router.post("/internal/invalidate-day")
def internal_invalidate_day(
    date: str,
    background_tasks: BackgroundTasks,
    auto_visit_svc=Depends(get_auto_visit_service),
    db_saver: DbSaver = Depends(get_db_saver),
) -> dict[str, Any]:
    """Drop AutoVisitService's cached sessions for ``date`` AND repair stale rows.

    Called by the demand_forecasting retrain cascade after fresh recs land in
    yf_recommended_orders. Two effects:

      1. **Cache drop (sync)** -- AutoVisitService's _sessions cache for that
         date is dropped so the next reconcile tick re-hydrates from the
         canonical DB state. Sub-millisecond; runs inline. Deliberately does
         NOT touch the route-handler _sessions LRU: that holds the in-flight
         session object backing an open supervisor's /visit calls, and
         dropping it mid-route would 404 the next tap.

      2. **DB repair (async)** -- the supervision-side backfill SQL is
         scheduled as a BackgroundTask so the DF cascade isn't blocked by
         a 30-60s 5-stage MERGE. ``repair_supervision_day`` is idempotent
         and decorated with ``@with_db_retry`` (deadlock-victim 1205 retries
         transparently), so a transient failure during the background run
         heals on its own.

    Repair is idempotent: running it twice has no effect on rows that are
    already correct. Putting the repair in a BackgroundTask means the
    response returns in ~milliseconds rather than blocking the DF cron
    cascade timeout.
    """
    if auto_visit_svc is None:
        return {"success": False, "reason": "auto_visit_service_disabled"}
    dropped = auto_visit_svc.invalidate_all_for_date(date)

    repair_scheduled = False
    if db_saver is not None and getattr(db_saver, "available", False):
        def _run_repair(d: str) -> None:
            try:
                db_saver.repair_supervision_day(d)
            except Exception as exc:
                logger.warning("repair_supervision_day failed for date=%s: %s", d, exc)
        background_tasks.add_task(_run_repair, date)
        repair_scheduled = True
    return {
        "success": True,
        "date": date,
        "auto_visit_dropped": dropped,
        "repair_scheduled": repair_scheduled,
    }


@router.post("/visit", response_model=VisitResponse)
def process_visit(
    req: ProcessVisitRequest,
    background_tasks: BackgroundTasks,
    mgr: SessionManager = Depends(get_session_manager),
    live: LiveActualsClient = Depends(get_live_actuals),
    db_saver: DbSaver = Depends(get_db_saver),
):
    """Mark a customer visited and persist to YaumiAIML via BackgroundTasks.

    Actuals are pulled live from YaumiLive; the client never supplies them.
    BackgroundTasks handle the DB upsert + saved-visits cache invalidation.
    LLM analyses are generated on-demand by the webapp at click-time.
    """
    session = _sessions.get(req.session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail=f"Session {req.session_id} not found",
        )

    customer = session.customers.get(req.customer_code)
    if customer is None:
        raise HTTPException(
            status_code=404,
            detail=f"Customer {req.customer_code} not in session",
        )

    # Lock serialises in-memory mutation across rapid /visit taps; DB upsert runs outside (idempotent).
    with _sessions.lock_for(req.session_id):
        actual_sales = live.get_actuals(session.route_code, session.date, req.customer_code)

        # Off-plan purchases ride through customer.items with rec=0/tier=UNPLANNED so saved-visits hydration mirrors live.
        planned_item_codes = {it.item_code for it in customer.items}
        also_bought_rows = [
            AlsoBoughtRow(item_code=code, qty=int(qty))
            for code, qty in actual_sales.items()
            if code not in planned_item_codes and int(qty or 0) > 0
        ]
        also_bought_rows.sort(key=lambda r: r.qty, reverse=True)

        result = mgr.process_visit(session, req.customer_code, actual_sales)

        for row in also_bought_rows:
            customer.items.append(SessionItem(
                item_code=row.item_code,
                item_name="",
                recommended_qty=0,
                actual_qty=int(row.qty),
                was_sold=True,
                tier=TIER_UNPLANNED,
                raw={
                    "ItemCode": row.item_code,
                    "ItemName": "",
                    "RecommendedQuantity": 0,
                    "Tier": TIER_UNPLANNED,
                },
            ))

        # Cumulative buffer ledger so this visit's allocation accounts for every earlier visit.
        buffer_ledger = compute_buffer_ledger(session)
        redistribution_view = shape_redistribution_view(
            session, req.customer_code, is_drop_in=False,
            buffer_ledger=buffer_ledger,
        )

        # Snapshot under the lock so the background upsert sees a coherent view.
        snapshot = session.to_dict()
        session_totals = snapshot["visit_totals"]
        actual_qty = customer.total_actual
        recommended_qty = customer.total_recommended

    if db_saver.available:
        background_tasks.add_task(
            db_saver.upsert_visit,
            snapshot,
            req.customer_code,
        )
        # Invalidate saved-visits cache AFTER upsert (FastAPI runs BackgroundTasks sequentially).
        background_tasks.add_task(
            db_saver.invalidate_saved_visits,
            session.route_code,
            session.date,
        )
        # LLM analyses are generated on-demand by the webapp at click-time;
        # the cron no longer fires them, and there's no DB column to persist
        # to. Visit completion is purely a data-write event now.

    visit_payload = VisitResultPayload(
        score=VisitScore(
            score=result.score.score,
            coverage=result.score.coverage,
            accuracy=result.score.accuracy,
        ),
        actualSales={k: int(v) for k, v in actual_sales.items()},
        actualQty=int(actual_qty),
        recommendedQty=int(recommended_qty),
        alsoBought=also_bought_rows,
        redistributions=redistribution_view,
        sessionTotals=session_totals,
    )
    return VisitResponse(success=True, visit=visit_payload)


# LLM save endpoints removed -- analyses are generated on-demand by the
# webapp calling llm_analytics directly; we no longer persist LLM output
# to the supervision DB. The /briefing, /customer-analysis, /route-analysis
# save routes are gone, along with their DbSaver counterparts.


@router.get("/saved", response_model=SavedVisitsResponse)
def saved_visits(
    route_code: str,
    date: str,
    include_redistributions: bool = False,
    db_saver: DbSaver = Depends(get_db_saver),
):
    """Saved visit data for (route, date) keyed by customer_code; default skips redistribution replay."""
    if not db_saver.available:
        return SavedVisitsResponse(available=False)
    payload = db_saver.load_session_visits(
        route_code, date, include_redistributions=include_redistributions,
    )
    if not payload:
        return SavedVisitsResponse(available=False)
    return SavedVisitsResponse(**payload)


@router.get("/redistribution/{route_code}/{date}/{customer_code}")
def redistribution_for_customer(
    route_code: str,
    date: str,
    customer_code: str,
    db_saver: DbSaver = Depends(get_db_saver),
):
    """On-demand single-customer redistribution replay (drill-in modal only)."""
    if not db_saver.available:
        return {"available": False}
    view = db_saver.load_redistribution_for_customer(route_code, date, customer_code)
    if view is None:
        return {"available": False}
    return {"available": True, "redistributions": view}


@router.get("/unplanned/{session_id}", response_model=UnplannedVisitsResponse)
def get_unplanned_visits(
    session_id: str,
    live: LiveActualsClient = Depends(get_live_actuals),
):
    """Drop-in customers for this session; also surfaces planned_visited_codes for the live-dot tile."""
    session = _sessions.get(session_id)
    if session is None:
        # Empty route_code/date because schema requires non-null strings on the error branch.
        return UnplannedVisitsResponse(
            success=False,
            error=f"Session {session_id} not found",
            route_code="",
            date="",
        )

    planned = {str(c).strip() for c in session.customers.keys()}
    visitors = live.get_route_sales(session.route_code, session.date)

    planned_visited: list[str] = []
    unplanned: list[dict] = []
    # Collect by customer_code so all redistribution views compute in one shaper pass at the end.
    dropin_items_per_customer: dict[str, list[dict[str, Any]]] = {}
    for v in visitors:
        code = str(v.get("customer_code", "")).strip()
        if not code:
            continue
        if code in planned:
            planned_visited.append(code)
        else:
            items = list(v.get("items") or [])
            v["total_qty"] = sum(int(it.get("qty") or 0) for it in items)
            v["unique_skus"] = len(items)
            v["live_visited"] = True
            dropin_items_per_customer[code] = items
            unplanned.append(v)
    unplanned.sort(key=lambda v: v.get("total_qty", 0), reverse=True)

    # Items are server-derived from the YaumiLive cut-through above, never client-supplied.
    views = compute_redistribution_for_unplanned(
        session, dropin_items_per_customer,
    )
    for v in unplanned:
        code = str(v.get("customer_code", "")).strip()
        view = views.get(code)
        v["redistributions"] = (
            view.model_dump()
            if view is not None
            else RedistributionView().model_dump()
        )

    return UnplannedVisitsResponse(
        success=True,
        route_code=session.route_code,
        date=session.date,
        planned_count=len(planned),
        live_count=len(visitors),
        unplanned_count=len(unplanned),
        planned_visited_codes=planned_visited,
        customers=unplanned,
    )
