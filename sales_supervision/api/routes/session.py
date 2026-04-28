"""
Session lifecycle: initialize -> visit -> save-active. Plus the live
unplanned-visits poll the live UI uses to flag drop-ins. Everything else
(update-actuals, get-summary, full-session, route-score) had no UI
consumer and was dropped to keep the surface minimal.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from sales_supervision.api.dependencies import (
    get_db_saver,
    get_live_actuals,
    get_session_manager,
    get_store,
)
from sales_supervision.api.schemas import (
    InitSessionRequest,
    ProcessVisitRequest,
    SessionResponse,
    VisitResponse,
)
from sales_supervision.core.session import SessionManager
from sales_supervision.services.db_saver import DbSaver
from sales_supervision.services.live_actuals import LiveActualsClient
from sales_supervision.services.storage.store import SessionStore

router = APIRouter(prefix="/session", tags=["session"])

# In-memory active sessions, keyed by session_id. Entries are dropped
# when /save-active completes so a long-lived process won't accumulate
# completed sessions.
_sessions: dict = {}


@router.post("/initialize", response_model=SessionResponse)
def initialize_session(
    req: InitSessionRequest,
    mgr: SessionManager = Depends(get_session_manager),
):
    session = mgr.create_session(req.route_code, req.date, req.recommendations)
    _sessions[session.session_id] = session
    return SessionResponse(success=True, session=session.summary())


@router.post("/visit", response_model=VisitResponse)
def process_visit(
    req: ProcessVisitRequest,
    mgr: SessionManager = Depends(get_session_manager),
    live: LiveActualsClient = Depends(get_live_actuals),
):
    """Mark a customer visited. Per-item actuals are pulled live from
    YaumiLive via data_import -- the client never supplies them."""
    session = _sessions.get(req.session_id)
    if session is None:
        return VisitResponse(success=False, visit={"error": f"Session {req.session_id} not found"})

    customer = session.customers.get(req.customer_code)
    if customer is None:
        return VisitResponse(success=False, visit={"error": f"Customer {req.customer_code} not in session"})

    actual_sales = live.get_actuals(session.route_code, session.date, req.customer_code)

    # Scoring evaluates planned items only; surface any extras the
    # customer bought as ``alsoBought`` so the UI can show them as
    # awareness-only context (no score impact).
    planned_item_codes = {it.item_code for it in customer.items}
    also_bought = [
        {"item_code": code, "qty": qty}
        for code, qty in actual_sales.items()
        if code not in planned_item_codes and qty > 0
    ]
    also_bought.sort(key=lambda r: r["qty"], reverse=True)

    result = mgr.process_visit(session, req.customer_code, actual_sales)
    payload = result.to_dict()
    payload["actualSales"] = actual_sales
    payload["alsoBought"] = also_bought
    payload["actualFetchedFromLive"] = True
    return VisitResponse(success=True, visit=payload)


@router.post("/save-active")
def save_active_session(
    session_id: str,
    mgr: SessionManager = Depends(get_session_manager),
    store: SessionStore = Depends(get_store),
    db_saver: DbSaver = Depends(get_db_saver),
):
    """Persist the active in-memory session to disk + DB (when configured)
    and free its slot in the in-memory registry.

    File save is required; DB save is best-effort when configured. The
    session stays in the in-memory registry on file-save failure so the
    supervisor can retry. On DB-only failure we still drop the in-memory
    slot (the JSON on disk is the source of truth) and flag the warning
    in the response so the UI can surface it.
    """
    session = _sessions.get(session_id)
    if session is None:
        return {"success": False, "error": f"Session {session_id} not found"}

    mgr.close_session(session)
    data = session.to_dict()

    file_result = store.save(data)
    if not file_result.get("success"):
        return {
            "success": False,
            "error": file_result.get("error", "File save failed"),
            "file": file_result,
            "db": None,
        }

    db_result = db_saver.save_session(data) if db_saver.available else None
    db_ok = db_result is None or db_result.get("success", False)

    _sessions.pop(session_id, None)
    return {
        "success": True,
        "db_ok": db_ok,
        "warning": (
            None if db_ok
            else f"Session saved to disk but DB write failed: {db_result.get('error', 'unknown')}"
        ),
        "file": file_result,
        "db": db_result,
    }


@router.get("/unplanned/{session_id}")
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
        return {"success": False, "error": f"Session {session_id} not found", "customers": []}

    planned = {str(c).strip() for c in session.customers.keys()}
    visitors = live.get_route_sales(session.route_code, session.date)

    planned_visited: list[str] = []
    unplanned: list[dict] = []
    for v in visitors:
        code = str(v.get("customer_code", "")).strip()
        if not code:
            continue
        if code in planned:
            planned_visited.append(code)
        else:
            v["total_qty"] = sum(int(it.get("qty") or 0) for it in v.get("items", []))
            unplanned.append(v)
    unplanned.sort(key=lambda v: v.get("total_qty", 0), reverse=True)

    return {
        "success": True,
        "route_code": session.route_code,
        "date": session.date,
        "planned_count": len(planned),
        "live_count": len(visitors),
        "unplanned_count": len(unplanned),
        "planned_visited_codes": planned_visited,
        "customers": unplanned,
    }
