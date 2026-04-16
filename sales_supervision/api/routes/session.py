"""
Session endpoints -- create, process visit, update, save, get summary.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from sales_supervision.api.dependencies import get_db_saver, get_live_actuals, get_session_manager, get_store
from sales_supervision.services.db_saver import DbSaver
from sales_supervision.services.live_actuals import LiveActualsClient
from sales_supervision.api.schemas import (
    InitSessionRequest,
    ProcessVisitRequest,
    SaveSessionRequest,
    SessionResponse,
    UpdateActualsRequest,
    VisitResponse,
    ScoreResponse,
)
from sales_supervision.core.session import SessionManager
from sales_supervision.services.storage.store import SessionStore

router = APIRouter(prefix="/session", tags=["session"])

# In-memory session registry (keyed by session_id)
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
    """Mark a customer as visited. Actual per-item quantities are fetched LIVE
    from YaumiLive via data_import -- the client does not supply them."""
    session = _sessions.get(req.session_id)
    if session is None:
        return VisitResponse(success=False, visit={"error": f"Session {req.session_id} not found"})

    customer = session.customers.get(req.customer_code)
    if customer is None:
        return VisitResponse(success=False, visit={"error": f"Customer {req.customer_code} not in session"})

    # Live fetch: exact (route, date, customer) match on YaumiLive.
    actual_sales = live.get_actuals(session.route_code, session.date, req.customer_code)

    # Scoring is evaluated per-planned-item (session.customer.items loop in the
    # processor), so passing the full actuals dict is safe: only planned items
    # can contribute to the score. Stray items are surfaced in ``alsoBought``
    # for operator awareness -- "customer bought stuff we didn't plan for".
    planned_item_codes = {it.item_code for it in customer.items}
    also_bought = [
        {"item_code": code, "qty": qty}
        for code, qty in actual_sales.items()
        if code not in planned_item_codes and qty > 0
    ]
    also_bought.sort(key=lambda r: r["qty"], reverse=True)

    result = mgr.process_visit(session, req.customer_code, actual_sales)
    payload = result.to_dict()
    payload["actualSales"] = actual_sales          # full dict, incl. unplanned
    payload["alsoBought"] = also_bought            # unplanned only, sorted
    payload["actualFetchedFromLive"] = True
    return VisitResponse(success=True, visit=payload)


@router.post("/update-actuals", response_model=ScoreResponse)
def update_actuals(
    req: UpdateActualsRequest,
    mgr: SessionManager = Depends(get_session_manager),
):
    session = _sessions.get(req.session_id)
    if session is None:
        return ScoreResponse(success=False, score=0, coverage=0, accuracy=0)

    score = mgr.update_actuals(session, req.customer_code, req.actuals)
    return ScoreResponse(success=True, score=score.score, coverage=score.coverage, accuracy=score.accuracy)


@router.post("/save")
def save_session(
    req: SaveSessionRequest,
    store: SessionStore = Depends(get_store),
):
    result = store.save(req.session_data)
    return {"success": True, **result}


@router.post("/save-active")
def save_active_session(
    session_id: str,
    mgr: SessionManager = Depends(get_session_manager),
    store: SessionStore = Depends(get_store),
    db_saver: DbSaver = Depends(get_db_saver),
):
    """Save the active in-memory session to file + DB (if configured)."""
    session = _sessions.get(session_id)
    if session is None:
        return {"success": False, "error": f"Session {session_id} not found"}

    mgr.close_session(session)
    data = session.to_dict()

    # Save to local file
    file_result = store.save(data)

    # Save to DB if configured
    db_result = None
    if db_saver.available:
        db_result = db_saver.save_session(data)

    # Clean up memory
    _sessions.pop(session_id, None)
    return {"success": True, "file": file_result, "db": db_result}


@router.get("/summary/{session_id}")
def get_session_summary(session_id: str):
    session = _sessions.get(session_id)
    if session is None:
        return {"success": False, "error": f"Session {session_id} not found"}
    return {"success": True, "session": session.summary()}


@router.get("/full/{session_id}")
def get_full_session(session_id: str):
    session = _sessions.get(session_id)
    if session is None:
        return {"success": False, "error": f"Session {session_id} not found"}
    return {"success": True, "session": session.to_dict()}


@router.get("/unplanned/{session_id}")
def get_unplanned_visits(
    session_id: str,
    live: LiveActualsClient = Depends(get_live_actuals),
):
    """Customers who had live invoices on this session's (route, date) but were
    NOT in the journey plan. Items are shown read-only -- no recommendation to
    score against."""
    session = _sessions.get(session_id)
    if session is None:
        return {"success": False, "error": f"Session {session_id} not found", "customers": []}

    route_code = session.route_code
    date = session.date
    planned = {str(c).strip() for c in session.customers.keys()}

    visitors = live.get_route_sales(route_code, date)

    # Single pass: split the live visitor set into planned-visited vs unplanned
    # so the UI can show a "visited live" indicator on planned customers without
    # a second backend round-trip.
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
        "route_code": route_code,
        "date": date,
        "planned_count": len(planned),
        "live_count": len(visitors),
        "unplanned_count": len(unplanned),
        "planned_visited_codes": planned_visited,
        "customers": unplanned,
    }


@router.get("/route-score/{session_id}")
def get_route_score(
    session_id: str,
    mgr: SessionManager = Depends(get_session_manager),
):
    session = _sessions.get(session_id)
    if session is None:
        return {"success": False, "error": f"Session {session_id} not found"}

    rs = mgr.route_score(session)
    return {
        "success": True,
        "routeScore": rs.route_score,
        "customerCoverage": rs.customer_coverage,
        "qtyFulfillment": rs.qty_fulfillment,
        "customerScores": rs.customer_scores,
    }
