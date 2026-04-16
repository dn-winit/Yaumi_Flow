"""
Session manager -- creates, loads, and manages supervision sessions.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sales_supervision.config.constants import SupervisionConstants
from sales_supervision.core.scoring import ScoringEngine
from sales_supervision.core.visit_processor import VisitProcessor
from sales_supervision.models.schemas import (
    RouteScoreResult,
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
        self._scorer = ScoringEngine(self._c)

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
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        uid = uuid.uuid4().hex[:6]
        session_id = f"{route_code}_{date}_{ts}_{uid}"

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
    # Update actual quantities (manual edit)
    # ------------------------------------------------------------------

    def update_actuals(
        self,
        session: Session,
        customer_code: str,
        actuals: Dict[str, int],
    ) -> ScoreResult:
        """Update actual quantities for a visited customer and re-score."""
        customer = session.customers.get(customer_code)
        if customer is None:
            raise ValueError(f"Customer {customer_code} not in session")

        for item in customer.items:
            if item.item_code in actuals:
                item.actual_qty = actuals[item.item_code]
                item.was_sold = actuals[item.item_code] > 0
                item.was_edited = True

        score = self._scorer.customer_score(customer)
        customer.score = score
        return score

    # ------------------------------------------------------------------
    # Route score
    # ------------------------------------------------------------------

    def route_score(self, session: Session) -> RouteScoreResult:
        return self._scorer.route_score(list(session.customers.values()))

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
                    item_code=it["itemCode"],
                    item_name=it.get("itemName", ""),
                    recommended_qty=int(it.get("recommendedQuantity", 0)),
                    actual_qty=int(it.get("actualQuantity", 0)),
                    adjustment=int(it.get("adjustment", 0)),
                    was_sold=bool(it.get("wasSold", False)),
                    was_edited=bool(it.get("wasEdited", False)),
                    tier=it.get("tier", ""),
                    priority_score=float(it.get("priorityScore", 0)),
                    days_since_last_purchase=int(it.get("daysSinceLastPurchase", 0)),
                    purchase_cycle_days=float(it.get("purchaseCycleDays", 0)),
                    frequency_percent=float(it.get("frequencyPercent", 0)),
                    van_inventory_qty=int(it.get("vanInventoryQty", 0)),
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
