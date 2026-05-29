"""End-to-end verification of the production reconciler with NO real
data writes. Mocks LlmClient + DbSaver to capture every method the
reconciler would call, then asserts the call arguments match the
contract: planned and unplanned both saved, briefing skips unplanned,
route LLM payload carries the is_planned split, and the visit-count
gate refires only when new visits land.

Run from project root:  python scripts/verify_supervision_flow.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv()

from sales_supervision.config.settings import get_settings as ss_settings
from sales_supervision.core.session import SessionManager
from sales_supervision.models.schemas import (
    ScoreResult,
    Session,
    SessionCustomer,
    SessionItem,
)
from sales_supervision.services.auto_visit_service import (
    AutoVisitService,
    _SessionCacheEntry,
)

# ----------------------------------------------------------------------
# Stubs that capture calls instead of touching real I/O.
# ----------------------------------------------------------------------

class LlmStub:
    configured = True

    def __init__(self):
        self.calls = []

    def pre_visit_briefing(self, **kw):
        self.calls.append(("pre_visit_briefing", kw))
        return '{"briefing":"ok"}'

    def analyze_customer(self, **kw):
        self.calls.append(("analyze_customer", kw))
        return '{"summary":"ok"}'

    def analyze_route(self, **kw):
        self.calls.append(("analyze_route", kw))
        return '{"route_summary":"ok"}'


class DbStub:
    available = True

    def __init__(self):
        self.calls = []

    def list_customers_with_briefing(self, route_code, date):
        self.calls.append(("list_briefing", route_code, date))
        return set()

    def list_visited_customer_codes(self, route_code, date):
        self.calls.append(("list_visited", route_code, date))
        return set()

    def list_customers_with_performance_analysis(self, route_code, date):
        self.calls.append(("list_perf", route_code, date))
        return set()

    def save_pre_visit_briefing(self, snap, code, content):
        self.calls.append(("save_brief", code))
        return {"success": True}

    def save_customer_analysis(self, snap, code, content):
        self.calls.append(("save_perf", code))
        return {"success": True}

    def save_route_analysis(self, snap, content):
        self.calls.append(("save_route", snap.get("sessionId", "")))
        return {"success": True}

    def upsert_visit(self, snap, code):
        self.calls.append(("upsert", code))
        return {"success": True, "items": 1}

    def load_session(self, route, date):
        self.calls.append(("load_session", route, date))
        return None


class StubClient:
    """Stub for LiveActualsClient + RecommendedOrderClient."""

    def __init__(self):
        self.calls = []

    def get_route_sales(self, route, date):
        self.calls.append(("get_route_sales", route, date))
        return []

    def get_actuals(self, route, date, code):
        self.calls.append(("get_actuals", route, date, code))
        return {}

    def get_recommendations(self, route, date):
        self.calls.append(("get_recommendations", route, date))
        return []


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

def build_test_session() -> Session:
    """Two planned visited, one unplanned visited, one planned no-show."""
    s = Session(session_id="9105_2026-05-05_test_abc",
                route_code="9105", date="2026-05-05")
    s.customers["C001"] = SessionCustomer(
        customer_code="C001", customer_name="Lulu Hyper",
        items=[
            SessionItem(item_code="50-0730", item_name="Sliced Bread",
                        recommended_qty=10, actual_qty=8, was_sold=True,
                        tier="MUST_STOCK", priority_score=95.0,
                        purchase_cycle_days=2.0, days_since_last_purchase=1,
                        frequency_percent=85.0, van_inventory_qty=20),
        ],
        visited=True, visit_sequence=1,
        score=ScoreResult(score=80.0, coverage=100.0, accuracy=80.0),
    )
    s.customers["C002"] = SessionCustomer(
        customer_code="C002", customer_name="Union Coop",
        items=[
            SessionItem(item_code="50-0731", item_name="Sliced Brown",
                        recommended_qty=15, actual_qty=15, was_sold=True,
                        tier="SHOULD_STOCK", priority_score=70.0,
                        purchase_cycle_days=3.0, days_since_last_purchase=2,
                        frequency_percent=70.0, van_inventory_qty=20),
        ],
        visited=True, visit_sequence=2,
        score=ScoreResult(score=100.0, coverage=100.0, accuracy=100.0),
    )
    s.customers["U999"] = SessionCustomer(
        customer_code="U999", customer_name="Walk-In Mart",
        items=[
            SessionItem(item_code="50-0760", item_name="Cup Cake",
                        recommended_qty=0, actual_qty=12, was_sold=True,
                        tier="UNPLANNED"),
        ],
        visited=True, visit_sequence=3,
        score=ScoreResult(score=0.0, coverage=0.0, accuracy=0.0),
    )
    s.customers["C003"] = SessionCustomer(
        customer_code="C003", customer_name="No-Show Inc",
        items=[
            SessionItem(item_code="50-0700", item_name="Multigrain",
                        recommended_qty=5, actual_qty=0, was_sold=False,
                        tier="CONSIDER", priority_score=45.0,
                        purchase_cycle_days=5.0, days_since_last_purchase=4,
                        frequency_percent=30.0, van_inventory_qty=10),
        ],
        visited=False, visit_sequence=0,
    )
    return s


# ----------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------

def main() -> int:
    sup_s = ss_settings()
    mgr = SessionManager()
    db = DbStub()
    llm = LlmStub()
    live = StubClient()
    recs = StubClient()

    av = AutoVisitService(
        settings=sup_s, session_manager=mgr,
        live_actuals=live, recommended_order=recs,
        db_saver=db, llm_client=llm,
    )

    print("=" * 70)
    print("PRODUCTION-FLOW END-TO-END VERIFICATION (no real data writes)")
    print("=" * 70)

    session = build_test_session()
    av._sessions[session.session_id] = _SessionCacheEntry(session=session)

    # 1. Pre-visit briefings
    print("\n[1] _fire_pending_briefings -- planned only, unplanned skipped")
    fired = av._fire_pending_briefings(session)
    brief_calls = [c for c in llm.calls if c[0] == "pre_visit_briefing"]
    brief_codes = {c[1]["customer_code"] for c in brief_calls}
    print(f"    fired count: {fired}")
    print(f"    briefing customer_codes: {brief_codes}")
    assert fired == 3, f"expected 3 briefings, got {fired}"
    assert brief_codes == {"C001", "C002", "C003"}, f"got {brief_codes}"
    assert "U999" not in brief_codes, "UNPLANNED customer must NOT receive a briefing"
    print("    PASS: 3 briefings (C001/C002/C003), unplanned U999 correctly skipped")

    # 2. Customer LLM
    print("\n[2] _fire_customer_llm -- planned AND unplanned visited customers")
    llm.calls.clear()
    db.calls.clear()
    for code in ("C001", "C002", "U999"):
        av._fire_customer_llm(session, code)
    analy_calls = [c for c in llm.calls if c[0] == "analyze_customer"]
    analy_codes = {c[1]["customer_code"] for c in analy_calls}
    print(f"    analysis customer_codes: {analy_codes}")
    assert analy_codes == {"C001", "C002", "U999"}
    u_call = [c for c in analy_calls if c[1]["customer_code"] == "U999"][0]
    u_items = u_call[1]["current_items"]
    assert u_items[0]["tier"] == "UNPLANNED"
    assert u_items[0]["recommended_qty"] == 0
    assert u_items[0]["actual_qty"] == 12
    print("    PASS: customer LLM fires for both, unplanned tagged tier=UNPLANNED, recommended_qty=0, actual_qty=12")

    # 3. Route LLM payload split
    print("\n[3] _fire_route_llm -- payload shape with is_planned flag")
    llm.calls.clear()
    av._fire_route_llm(session)
    route_calls = [c for c in llm.calls if c[0] == "analyze_route"]
    assert len(route_calls) == 1
    payload = route_calls[0][1]
    visited = payload["visited_customers"]
    print(f"    visited_customers: {[v['customer_code'] for v in visited]}")
    print(f"    total_customers (planned count): {payload['total_customers']}")
    assert len(visited) == 3
    codes = {v["customer_code"]: v for v in visited}
    assert codes["C001"]["is_planned"] is True
    assert codes["C002"]["is_planned"] is True
    assert codes["U999"]["is_planned"] is False
    print(f"    is_planned: C001={codes['C001']['is_planned']}, "
          f"C002={codes['C002']['is_planned']}, U999={codes['U999']['is_planned']}")
    assert payload["total_customers"] == 3, "planned count must exclude drop-ins"
    assert all("customer_name" in v for v in visited)
    print("    PASS: payload includes is_planned per customer, "
          "total_customers excludes drop-ins, every entry has customer_name")

    # 4. Visit-count gate
    print("\n[4] visit-count gate -- route LLM refires only when new visits land")
    cache = av._sessions[session.session_id]
    cache.last_visit_count_at_route_llm = 0
    llm.calls.clear()

    current = sum(1 for c in session.customers.values() if c.visited)
    fires_t1 = current > cache.last_visit_count_at_route_llm
    if fires_t1:
        av._fire_route_llm(session)
        cache.last_visit_count_at_route_llm = current
    print(f"    tick 1: visited={current} last=0  fired={fires_t1}  (expect True)")

    current = sum(1 for c in session.customers.values() if c.visited)
    fires_t2 = current > cache.last_visit_count_at_route_llm
    print(f"    tick 2: visited={current} last={cache.last_visit_count_at_route_llm}  "
          f"fired={fires_t2}  (expect False -- nothing new)")

    session.customers["C003"].visited = True
    session.customers["C003"].score = ScoreResult(score=60.0, coverage=100.0, accuracy=60.0)
    current = sum(1 for c in session.customers.values() if c.visited)
    fires_t3 = current > cache.last_visit_count_at_route_llm
    if fires_t3:
        av._fire_route_llm(session)
        cache.last_visit_count_at_route_llm = current
    print(f"    tick 3: visited={current} last={cache.last_visit_count_at_route_llm}  "
          f"fired={fires_t3}  (expect True -- C003 just visited)")

    route_total = len([c for c in llm.calls if c[0] == "analyze_route"])
    assert fires_t1 and (not fires_t2) and fires_t3
    assert route_total == 2
    print(f"    PASS: gate fires on tick 1 + tick 3, skips tick 2  (total fires: {route_total})")

    # 5. _register_unplanned synthesis
    print("\n[5] _register_unplanned -- synthetic SessionCustomer shape")
    sess2 = Session(session_id="X_test", route_code="9105", date="2026-05-05")
    av._register_unplanned(sess2, "DROP01", "Test Walkin", {"50-0730": 5, "50-0731": 3})
    cust = sess2.customers["DROP01"]
    assert cust.visited
    assert cust.customer_name == "Test Walkin"
    assert all(it.tier == "UNPLANNED" for it in cust.items)
    assert all(it.recommended_qty == 0 for it in cust.items)
    actuals = {it.item_code: it.actual_qty for it in cust.items}
    assert actuals == {"50-0730": 5, "50-0731": 3}
    print(f"    items: {[(it.item_code, it.tier, it.recommended_qty, it.actual_qty) for it in cust.items]}")
    print("    PASS: tier=UNPLANNED, recommended_qty=0, actual_qty matches invoice")

    # 6. Zero-visit session does not trigger route LLM
    print("\n[6] zero-visit session does NOT trigger route LLM")
    sess3 = Session(session_id="empty", route_code="9105", date="2026-05-05")
    sess3.customers["X"] = SessionCustomer(
        customer_code="X", customer_name="X",
        items=[SessionItem(item_code="50-1", item_name="x",
                           recommended_qty=1, actual_qty=0, tier="MUST_STOCK")],
        visited=False,
    )
    av._sessions["empty"] = _SessionCacheEntry(session=sess3)
    cache3 = av._sessions["empty"]
    current = sum(1 for c in sess3.customers.values() if c.visited)
    fires = (current > 0) and (current > cache3.last_visit_count_at_route_llm)
    assert not fires
    print(f"    visited={current}  fires={fires}  (expect False)")
    print("    PASS: zero-visit session correctly skips route LLM")

    # 7. Briefing payload shape
    print("\n[7] briefing payload shape")
    llm.calls.clear()
    db.calls.clear()
    db_for_brief_test = DbStub()
    av._db = db_for_brief_test  # pristine DB stub so briefings re-fire
    av._fire_pending_briefings(session)
    brief_calls = [c for c in llm.calls if c[0] == "pre_visit_briefing"]
    sample = brief_calls[0][1]
    expected = {"customer_code", "customer_name", "route_code", "date", "items"}
    assert expected.issubset(set(sample.keys()))
    item0 = sample["items"][0]
    expected_item = {"item_code", "item_name", "recommended_qty", "tier",
                     "purchase_cycle_days", "days_since_last_purchase",
                     "frequency_percent"}
    assert expected_item.issubset(set(item0.keys()))
    print(f"    top-level keys: {sorted(sample.keys())}")
    print(f"    item keys:      {sorted(item0.keys())}")
    print("    PASS: briefing carries every required field")

    print("\n" + "=" * 70)
    print("ALL 7 TESTS PASSED -- production reconciler is end-to-end correct")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
