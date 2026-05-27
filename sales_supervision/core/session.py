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
        # Deterministic sid per (route, date) -- makes route/customer/item upserts idempotent across page reloads.
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
        """Layer persisted visit state (actuals + score + alsoBought) onto a fresh session.

        Without this hydration ``session.summary().visit_totals`` starts at zero
        and ``/initialize`` disagrees with ``/saved`` on the headline tile.
        Customers present in the saved store but absent from today's plan are skipped.
        Returns the number of customers hydrated.
        """
        from sales_supervision.core.constants import TIER_UNPLANNED

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

            # Mirror live process_visit's alsoBought append; idempotent against existing codes.
            also_bought = sv.get("alsoBought") or []
            existing_codes = {it.item_code for it in cust.items}
            for ab in also_bought:
                code = str((ab.get("item_code") or "")).strip()
                qty = int(ab.get("qty", 0) or 0)
                if not code or qty <= 0 or code in existing_codes:
                    continue
                cust.items.append(SessionItem(
                    item_code=code,
                    item_name="",
                    recommended_qty=0,
                    actual_qty=qty,
                    was_sold=True,
                    tier=TIER_UNPLANNED,
                    raw={
                        "ItemCode": code,
                        "ItemName": "",
                        "RecommendedQuantity": 0,
                        "Tier": TIER_UNPLANNED,
                    },
                ))
                existing_codes.add(code)

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
            )

        return Session(
            session_id=data.get("sessionId", ""),
            route_code=data.get("routeCode", ""),
            date=data.get("date", ""),
            customers=customers,
            status=data.get("status", "closed"),
        )
