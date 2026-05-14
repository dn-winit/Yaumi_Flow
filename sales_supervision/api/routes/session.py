"""
Session lifecycle: initialize -> visit (auto-persisted). Plus the live
unplanned-visits poll the live UI uses to flag drop-ins.

Every ``process_visit`` call upserts the route header + the visited
customer + that customer's items into YaumiAIML in the background.
There is no separate "save session" step -- the DB stays in sync with
the in-memory session as visits land.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional

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
    LlmSaveResponse,
    ProcessVisitRequest,
    RedistributionView,
    SaveBriefingRequest,
    SaveCustomerAnalysisRequest,
    SaveRouteAnalysisRequest,
    SavedVisitsResponse,
    SessionResponse,
    SessionSummary,
    UnplannedVisitsResponse,
    VisitResponse,
    VisitResultPayload,
    VisitScore,
)
from sales_supervision.core.constants import TIER_UNPLANNED
from sales_supervision.models.schemas import SessionItem
from sales_supervision.core.redistribution import (
    compute_buffer_ledger,
    compute_redistribution_for_unplanned,
    shape_redistribution_view,
)
from sales_supervision.core.session import SessionManager
from sales_supervision.services.db_saver import DbSaver
from sales_supervision.services.live_actuals import LiveActualsClient

router = APIRouter(prefix="/session", tags=["session"])


# In-memory active sessions, keyed by session_id. Without a save the
# entry would otherwise live forever in a long-running process and leak
# memory. The registry caps both concurrent in-memory sessions and how
# long an idle session is held; both come from Settings so deployments
# with longer shifts (12-hour depots, weekend coverage) tune via env
# vars without code changes.
from sales_supervision.config.settings import get_settings as _get_ss_settings


class _SessionRegistry:
    """LRU + TTL store for in-flight supervision sessions.

    Also vends a per-session ``threading.Lock`` so concurrent ``/visit``
    requests on the same session serialise their in-memory mutations.
    Without this, a supervisor double-tap on the same customer (or a
    rapid sequence of taps across customers) interleaves
    ``mgr.process_visit`` writes to ``session.customers[*]`` and the
    final session state -- and the snapshot the background DB-upsert
    captures -- becomes non-deterministic.
    """

    def __init__(self, maxsize: int, ttl_seconds: int) -> None:
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._items: "OrderedDict[str, tuple[float, object]]" = OrderedDict()
        # Locks live alongside sessions and are evicted with them. The
        # outer mutex protects the locks dict from concurrent allocate
        # / pop; each per-session lock is held only during a single
        # visit's in-memory mutation + snapshot.
        self._locks: Dict[str, threading.Lock] = {}
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
        # Refresh access time so an actively-used session does not get
        # evicted purely because it was created early in the day.
        self._items[session_id] = (time.time(), session)
        self._items.move_to_end(session_id)
        return session

    def pop(self, session_id: str) -> None:
        self._items.pop(session_id, None)
        self._drop_lock(session_id)


_ss = _get_ss_settings()
_sessions = _SessionRegistry(_ss.session_registry_max, _ss.session_ttl_seconds)


@router.post("/initialize", response_model=SessionResponse)
def initialize_session(
    req: InitSessionRequest,
    background_tasks: BackgroundTasks,
    mgr: SessionManager = Depends(get_session_manager),
    db_saver: DbSaver = Depends(get_db_saver),
    auto_visit_svc=Depends(get_auto_visit_service),
):
    # Page-open is READ-ONLY: hydrate from whatever the cron has already
    # persisted, return immediately. Reconciliation is fired as a
    # BackgroundTask so any post-last-tick YaumiLive activity catches up
    # in <60s via the saved-visits poll; the frontend's ``countsReady``
    # gate keeps the tile in skeleton until /session/saved confirms, so
    # the supervisor never sees stale numbers. Invalidating the cron's
    # cached session here forces its next tick to pick up the journey
    # plan that's current at page-open time.
    if auto_visit_svc is not None and db_saver.available:
        auto_visit_svc.invalidate_route(req.route_code, req.date)
        background_tasks.add_task(
            auto_visit_svc.reconcile_route,
            req.route_code, req.date, skip_llm=True,
        )

    # Recommendations: client-supplied take precedence (freshest after
    # a manual regenerate); server falls back to recommended_order so
    # the wire contract stays optional. Single source either way.
    recs = req.recommendations
    if not recs and auto_visit_svc is not None:
        recs = auto_visit_svc._recs.get_recommendations(req.route_code, req.date) or []
    session = mgr.create_session(req.route_code, req.date, recs)
    if db_saver.available:
        saved = db_saver.load_session_visits(req.route_code, req.date)
        if saved:
            mgr.hydrate_saved_visits(session, saved)
    _sessions.set(session.session_id, session)
    return SessionResponse(
        success=True,
        session=SessionSummary(**session.summary()),
    )


@router.post("/visit", response_model=VisitResponse)
def process_visit(
    req: ProcessVisitRequest,
    background_tasks: BackgroundTasks,
    mgr: SessionManager = Depends(get_session_manager),
    live: LiveActualsClient = Depends(get_live_actuals),
    db_saver: DbSaver = Depends(get_db_saver),
    auto_visit_svc=Depends(get_auto_visit_service),
):
    """Mark a customer visited and persist the visit to YaumiAIML.

    Per-item actuals are pulled live from YaumiLive via data_import --
    the client never supplies them. Once the in-memory session is
    updated, the route header, the visited customer's row, and that
    customer's item rows are upserted in a single transaction. The
    upsert runs as a FastAPI ``BackgroundTask`` so the response returns
    to the field UI immediately and warehouse latency never blocks a
    visit tap. A second BackgroundTask then fires the LLM briefing +
    customer analysis so every column in the supervision tables --
    including the LLM ones -- is populated within seconds of the
    green-dot moment.
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

    # Per-session lock serialises in-memory mutation across concurrent
    # ``/visit`` calls (supervisor double-tap, rapid-fire taps across
    # customers). Lock duration covers the live-actuals fetch, the
    # in-memory ``mgr.process_visit`` write, and the snapshot capture
    # for the background upsert. The DB upsert itself runs outside the
    # lock because it's already idempotent on (session_id, customer_code).
    with _sessions.lock_for(req.session_id):
        actual_sales = live.get_actuals(session.route_code, session.date, req.customer_code)

        # Scoring evaluates planned items only; off-plan purchases are
        # surfaced as ``alsoBought`` for the live UI and ALSO appended to
        # ``customer.items`` with ``recommended_qty=0, tier=UNPLANNED`` so
        # they ride through ``upsert_visit`` into ``yf_supervision_items``.
        # That lets the saved-visits hydration emit them again on page
        # reload (same wire shape as the live response), so off-plan
        # purchases never silently disappear after a refresh.
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

        # Cumulative buffer ledger across the session's visited walk so
        # this visit's allocation decisions reflect EVERY earlier visit's
        # deposits and withdrawals. Single pass per /visit tap; cheap
        # relative to the live-actuals fetch. The ledger is consumed
        # server-side only -- buffer state is not surfaced on the wire.
        buffer_ledger = compute_buffer_ledger(session)
        redistribution_view = shape_redistribution_view(
            session, req.customer_code, is_drop_in=False,
            buffer_ledger=buffer_ledger,
        )

        # Snapshot the current session state under the lock so the
        # background upsert sees a coherent view.
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
        # Fire briefing + customer LLM right after the visit row lands so
        # the green-dot moment populates every supervision column,
        # including the LLM ones. FastAPI runs BackgroundTasks
        # sequentially, so ``upsert_visit`` completes before this fires
        # and the saver sees the just-persisted row. The 60s cron stays
        # as a safety net for any visit that lands while this service is
        # unreachable.
        if auto_visit_svc is not None:
            background_tasks.add_task(
                auto_visit_svc.fire_llms_for_visit,
                session,
                req.customer_code,
            )

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


# ----------------------------------------------------------------------
# LLM-payload persistence
#
# Three artifacts, three endpoints. Each runs the actual DB write as a
# FastAPI ``BackgroundTask`` so the UI returns immediately -- the LLM
# response is already on screen by the time this fires. Body shape is
# always (session_id, [customer_code], content); ``content`` is the
# JSON-stringified analytics payload, stored as-is so a future schema
# change in the LLM response doesn't require a DB migration.
# ----------------------------------------------------------------------


def _snapshot_or_error(
    session_id: str, db_saver: Optional[DbSaver] = None,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Locate the session and return its dict snapshot.

    Order of resolution:
      1. In-memory registry -- the live, hot path.
      2. DB rebuild from the saved (route, date) header -- covers the
         case where the supervisor saves an LLM payload after the
         session has been evicted from the registry (TTL expiry, server
         restart, or simply a re-opened tab on a previously-closed
         session). Without this fallback, a 200 OK from the analyzer
         would silently fail to persist and the row would stay NULL.

    Returned tuple is ``(snapshot, error)`` -- exactly one is non-None.
    """
    session = _sessions.get(session_id)
    if session is not None:
        return session.to_dict(), None
    if db_saver is not None and db_saver.available:
        snap = db_saver.load_session_by_id(session_id)
        if snap is not None:
            return snap, None
    return None, f"Session {session_id} not found"


@router.post("/briefing", response_model=LlmSaveResponse)
def save_pre_visit_briefing(
    req: SaveBriefingRequest,
    background_tasks: BackgroundTasks,
    db_saver: DbSaver = Depends(get_db_saver),
):
    snapshot, err = _snapshot_or_error(req.session_id, db_saver)
    if err or snapshot is None:
        return LlmSaveResponse(success=False, error=err)
    if not db_saver.available:
        return LlmSaveResponse(success=True)  # silently no-op; not a UI failure
    background_tasks.add_task(
        db_saver.save_pre_visit_briefing,
        snapshot, req.customer_code, req.content,
    )
    return LlmSaveResponse(success=True)


@router.post("/customer-analysis", response_model=LlmSaveResponse)
def save_customer_analysis(
    req: SaveCustomerAnalysisRequest,
    background_tasks: BackgroundTasks,
    db_saver: DbSaver = Depends(get_db_saver),
):
    snapshot, err = _snapshot_or_error(req.session_id, db_saver)
    if err or snapshot is None:
        return LlmSaveResponse(success=False, error=err)
    if not db_saver.available:
        return LlmSaveResponse(success=True)
    background_tasks.add_task(
        db_saver.save_customer_analysis,
        snapshot, req.customer_code, req.content,
    )
    return LlmSaveResponse(success=True)


@router.post("/route-analysis", response_model=LlmSaveResponse)
def save_route_analysis(
    req: SaveRouteAnalysisRequest,
    background_tasks: BackgroundTasks,
    db_saver: DbSaver = Depends(get_db_saver),
):
    snapshot, err = _snapshot_or_error(req.session_id, db_saver)
    if err or snapshot is None:
        return LlmSaveResponse(success=False, error=err)
    if not db_saver.available:
        return LlmSaveResponse(success=True)
    background_tasks.add_task(db_saver.save_route_analysis, snapshot, req.content)
    return LlmSaveResponse(success=True)


@router.get("/saved", response_model=SavedVisitsResponse)
def saved_visits(
    route_code: str,
    date: str,
    include_redistributions: bool = False,
    db_saver: DbSaver = Depends(get_db_saver),
):
    """Already-saved visit data for a (route, date), keyed by
    customer_code. Used by the live UI on mount so a customer with a
    prior visit renders their actuals + score immediately, without
    re-running the briefing -> mark-visited flow.

    ``include_redistributions=False`` (default) skips the expensive
    per-customer replay; drill-in fetches one customer at a time via
    ``GET /session/redistribution/{route}/{date}/{customer_code}``.
    Set to True only when the caller needs every visit's replay in one
    response (legacy clients / one-off audit scripts).
    """
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
    """On-demand redistribution replay for one customer. The saved-visits
    hot path skips replay to keep polling cheap; the supervisor's
    per-customer drill-in modal fires this once on open."""
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
    """Customers who invoiced live on this session's (route, date) but
    weren't on the journey plan. Splits the live visitor set into
    ``planned_visited_codes`` (so planned tiles can show a live dot
    without a second round-trip) and ``customers`` (the unplanned list).
    """
    session = _sessions.get(session_id)
    if session is None:
        # Pydantic now requires ``route_code`` + ``date`` as strings, so
        # the error branch emits empty strings rather than nulls. The
        # caller surfaces ``error`` to the supervisor; the empty IDs
        # make ``!success`` rendering unambiguous.
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
    # Collect drop-in items keyed by customer_code so we can compute
    # every redistribution view in a single shaper pass at the end --
    # avoids building an intermediate session per customer inside the
    # request loop.
    dropin_items_per_customer: Dict[str, list[Dict[str, Any]]] = {}
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

    # Shape each drop-in's view against the current session's
    # downstream planned pool. Items are server-provided (lifted from
    # the YaumiLive cut-through above), never client-supplied.
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
