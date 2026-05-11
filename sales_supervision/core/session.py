"""
Session manager -- creates, loads, and manages supervision sessions.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sales_supervision.config.constants import SupervisionConstants
from sales_supervision.core.visit_processor import VisitProcessor
from sales_supervision.models.schemas import (
    ScoreResult,
    Session,
    SessionCustomer,
    SessionItem,
    VisitResult,
)

logger = logging.getLogger(__name__)


class SessionManager:
    """Creates and manages supervision sessions."""

    def __init__(self, constants: Optional[SupervisionConstants] = None) -> None:
        self._c = constants or SupervisionConstants()
        self._processor = VisitProcessor(self._c)

    # ------------------------------------------------------------------
    # Create session from recommendations
    # ------------------------------------------------------------------

    def create_session(
        self,
        route_code: str,
        date: str,
        recommendations: List[Dict[str, Any]],
    ) -> Session:
        """
        Build a Session from recommendation records.

        Each record should have: CustomerCode, CustomerName, ItemCode, ItemName,
        RecommendedQuantity, Tier, PriorityScore, DaysSinceLastPurchase,
        PurchaseCycleDays, FrequencyPercent, VanLoad.
        """
        # Deterministic ``session_id`` for (route, date). Earlier this
        # method tacked on a per-process timestamp + uuid, which caused
        # every page-load to mint a brand-new sid; the route header,
        # customer summary, and item details tables then accumulated a
        # parallel session row per page-open. ``/session/saved`` reads
        # ``TOP 1 ORDER BY session_started_at DESC`` -- the newest of
        # those parallel sids -- and lost every visit that landed under
        # an older one, surfacing as the live "Visited X/Y" tile ticking
        # 1, 2, 3 in front of the supervisor as the auto-fire effect
        # walked the missing customers back in one at a time. One sid
        # per (route, date) makes ``_upsert_route_header`` and the
        # customer/item upserts (keyed on the same composite) naturally
        # idempotent -- every page-load lands on the same row.
        session_id = f"{route_code}_{date}"

        customers: Dict[str, SessionCustomer] = {}

        for rec in recommendations:
            ccode = str(rec.get("CustomerCode", ""))
            if not ccode:
                continue

            if ccode not in customers:
                customers[ccode] = SessionCustomer(
                    customer_code=ccode,
                    customer_name=str(rec.get("CustomerName", "")),
                )

            customers[ccode].items.append(SessionItem(
                item_code=str(rec.get("ItemCode", "")),
                item_name=str(rec.get("ItemName", "")),
                recommended_qty=int(rec.get("RecommendedQuantity", 0)),
                tier=str(rec.get("Tier", "")),
                priority_score=float(rec.get("PriorityScore", 0)),
                days_since_last_purchase=int(rec.get("DaysSinceLastPurchase", 0)),
                purchase_cycle_days=float(rec.get("PurchaseCycleDays", 0)),
                frequency_percent=float(rec.get("FrequencyPercent", 0)),
                van_inventory_qty=int(rec.get("VanLoad", 0)),
                raw=rec,
            ))

        session = Session(
            session_id=session_id,
            route_code=route_code,
            date=date,
            customers=customers,
        )

        logger.info(
            "Session created: %s -- %d customers, %d items",
            session_id, len(customers),
            sum(len(c.items) for c in customers.values()),
        )
        return session

    # ------------------------------------------------------------------
    # Hydrate previously-persisted visits onto a fresh session
    # ------------------------------------------------------------------

    def hydrate_saved_visits(
        self,
        session: Session,
        saved: Dict[str, Any],
    ) -> int:
        """Apply persisted visit state onto a freshly-created session.

        ``create_session`` builds an empty in-memory model from today's
        plan -- every customer is unvisited, every item has zero
        actuals. The supervision DB is the source of truth for the day's
        actual progress, so we layer the persisted actuals / score /
        visited flag onto the matching customers before the session
        starts serving requests.

        Without this hydration ``session.summary().visit_totals``
        starts at zero and each ``/visit`` response walks the counter up
        from 1, even when the saved store already shows 30/32 visited
        -- producing the visible "30 -> 1 -> 2 -> 3 ..." flicker on the
        live tile when the supervisor opens an in-progress route.

        ``saved`` is the dict returned by ``DbSaver.load_session_visits``
        (keyed under ``visits`` by customer code). Customers present in
        the saved store but absent from today's plan are skipped --
        their state is still served via ``/session/saved`` for the
        drill-in view but does not enter the session aggregates.

        Returns the number of customers hydrated.
        """
        visits = (saved or {}).get("visits") or {}
        if not visits:
            return 0
        seq = session.visit_sequence_counter
        hydrated = 0
        for ccode, sv in visits.items():
            cust = session.customers.get(str(ccode))
            if cust is None or not isinstance(sv, dict):
                continue
            actuals = sv.get("actualSales") or {}
            score_d = sv.get("score") or {}
            for item in cust.items:
                qty = int(actuals.get(item.item_code, 0) or 0)
                item.actual_qty = qty
                item.was_sold = qty > 0
            cust.score = ScoreResult(
                score=float(score_d.get("score", 0) or 0),
                coverage=float(score_d.get("coverage", 0) or 0),
                accuracy=float(score_d.get("accuracy", 0) or 0),
            )
            cust.visited = True
            seq += 1
            cust.visit_sequence = seq
            hydrated += 1
        if hydrated:
            logger.info(
                "Session hydrated: %s -- %d prior visits applied",
                session.session_id, hydrated,
            )
        return hydrated

    # ------------------------------------------------------------------
    # Process visit
    # ------------------------------------------------------------------

    def process_visit(
        self,
        session: Session,
        customer_code: str,
        actual_sales: Dict[str, int],
    ) -> VisitResult:
        return self._processor.process(session, customer_code, actual_sales)

    # ------------------------------------------------------------------
    # Close session
    # ------------------------------------------------------------------

    def close_session(self, session: Session) -> None:
        session.status = "closed"

    # ------------------------------------------------------------------
    # Rebuild session from stored data
    # ------------------------------------------------------------------

    def rebuild_session(self, data: Dict[str, Any]) -> Session:
        """Reconstruct a Session from stored dict (loaded from file/DB)."""
        customers: Dict[str, SessionCustomer] = {}

        for ccode, cdata in data.get("customers", {}).items():
            items = [
                SessionItem(
                    item_code=it.get("ItemCode", ""),
                    item_name=it.get("ItemName", ""),
                    recommended_qty=int(it.get("RecommendedQuantity", 0)),
                    actual_qty=int(it.get("ActualQuantity", 0)),
                    adjustment=int(it.get("Adjustment", 0)),
                    was_sold=bool(it.get("WasSold", False)),
                    was_edited=bool(it.get("WasEdited", False)),
                    tier=it.get("Tier", ""),
                    priority_score=float(it.get("PriorityScore", 0)),
                    days_since_last_purchase=int(it.get("DaysSinceLastPurchase", 0)),
                    purchase_cycle_days=float(it.get("PurchaseCycleDays", 0)),
                    frequency_percent=float(it.get("FrequencyPercent", 0)),
                    van_inventory_qty=int(it.get("VanLoad", 0)),
                    # Carry the full row through hydration so the modal sees
                    # WhyItem / WhyQuantity / Confidence / etc. on reload.
                    raw=it,
                )
                for it in cdata.get("items", [])
            ]

            sc = ScoreResult(
                score=float(cdata.get("score", 0)),
                coverage=float(cdata.get("coverage", 0)),
                accuracy=float(cdata.get("accuracy", 0)),
            )

            customers[ccode] = SessionCustomer(
                customer_code=ccode,
                customer_name=cdata.get("customerName", ""),
                items=items,
                visited=bool(cdata.get("visited", False)),
                visit_sequence=int(cdata.get("visitSequence", 0)),
                score=sc,
                llm_analysis=cdata.get("llmAnalysis", ""),
            )

        return Session(
            session_id=data.get("sessionId", ""),
            route_code=data.get("routeCode", ""),
            date=data.get("date", ""),
            customers=customers,
            status=data.get("status", "closed"),
        )
