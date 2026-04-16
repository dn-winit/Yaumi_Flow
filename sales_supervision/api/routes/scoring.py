"""
Scoring endpoints -- standalone score calculations + methodology.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from sales_supervision.api.dependencies import get_constants
from sales_supervision.api.schemas import (
    MethodologyResponse,
    RouteScoreResponse,
    ScoreCustomerRequest,
    ScoreRouteRequest,
)
from sales_supervision.config.constants import SupervisionConstants
from sales_supervision.core.scoring import ScoringEngine
from sales_supervision.models.schemas import SessionCustomer, SessionItem

router = APIRouter(prefix="/scoring", tags=["scoring"])


@router.post("/customer")
def score_customer(
    req: ScoreCustomerRequest,
    consts: SupervisionConstants = Depends(get_constants),
):
    """Calculate customer score from a list of items with actual/recommended."""
    scorer = ScoringEngine(consts)
    items = [
        SessionItem(
            item_code=it.get("itemCode", ""),
            item_name=it.get("itemName", ""),
            recommended_qty=int(it.get("recommendedQuantity", 0)),
            actual_qty=int(it.get("actualQuantity", 0)),
            was_sold=int(it.get("actualQuantity", 0)) > 0,
        )
        for it in req.items
    ]
    cust = SessionCustomer(customer_code="calc", items=items, visited=True)
    result = scorer.customer_score(cust)
    return {"success": True, "score": result.score, "coverage": result.coverage, "accuracy": result.accuracy}


@router.post("/route", response_model=RouteScoreResponse)
def score_route(
    req: ScoreRouteRequest,
    consts: SupervisionConstants = Depends(get_constants),
):
    """Calculate route score from a list of customers."""
    scorer = ScoringEngine(consts)
    customers = []
    for cd in req.customers:
        items = [
            SessionItem(
                item_code=it.get("itemCode", ""),
                item_name="",
                recommended_qty=int(it.get("recommendedQuantity", 0)),
                actual_qty=int(it.get("actualQuantity", 0)),
                was_sold=int(it.get("actualQuantity", 0)) > 0,
            )
            for it in cd.get("items", [])
        ]
        cust = SessionCustomer(
            customer_code=cd.get("customerCode", ""),
            items=items,
            visited=cd.get("visited", True),
        )
        cust.score = scorer.customer_score(cust)
        customers.append(cust)

    rs = scorer.route_score(customers)
    return RouteScoreResponse(
        success=True,
        route_score=rs.route_score,
        customer_coverage=rs.customer_coverage,
        qty_fulfillment=rs.qty_fulfillment,
        customer_scores=rs.customer_scores,
    )


@router.get("/methodology", response_model=MethodologyResponse)
def get_methodology(consts: SupervisionConstants = Depends(get_constants)):
    """Return the scoring formula details."""
    az = consts.accuracy
    sw = consts.scoring
    return MethodologyResponse(
        item_accuracy={
            "perfect_zone": f"{az.perfect_low*100:.0f}%-{az.perfect_high*100:.0f}%",
            "perfect_score": 100,
            "below_zone": f"Linear 0->100 as ratio 0->{az.perfect_low*100:.0f}%",
            "above_zone": f"Linear 100->0 as ratio {az.perfect_high*100:.0f}%->{az.max_over*100:.0f}%",
            "max_over": f"{az.max_over*100:.0f}%+ = 0 score",
        },
        customer_score={
            "formula": f"(SKU Coverage x {sw.coverage}) + (Avg Item Accuracy x {sw.accuracy})",
            "coverage": "items_sold / total_items x 100",
            "accuracy": "average of all item accuracies",
        },
        route_score={
            "formula": "average of visited customer scores",
            "customer_coverage": "visited / total planned x 100",
            "qty_fulfillment": "total actual / total recommended x 100",
        },
    )
