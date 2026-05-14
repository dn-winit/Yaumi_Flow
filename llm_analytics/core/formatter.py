"""
Data formatters -- convert DataFrames and dicts into prompt-ready text tables.

Input dicts may arrive in either snake_case (the supervision auto-path's
native shape) or camelCase / PascalCase (the browser-driven path's wire
shape). ``_pick`` accepts a list of candidate keys and returns the first
populated one. Earlier versions accepted only camelCase, which silently
collapsed every value to zero on the supervision side -- the bug behind
the contradictions in route summaries ("3566 units sold but 0% accuracy
across all customers"). Producers stay free to evolve their key
conventions; the formatter normalises at the boundary.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from llm_analytics.config.settings import Settings, get_settings


def _sanitize(text: str) -> str:
    return str(text).replace('"', "inch").replace("'", "").replace("\n", " ").replace("\r", " ")


def _pick(d: Dict[str, Any], *keys: str, default: Any = 0) -> Any:
    """Return the first non-None value from ``keys`` in ``d``.

    Lets one formatter serve both snake_case (supervision) and
    camelCase (browser) callers without forcing the caller to translate.
    """
    for k in keys:
        v = d.get(k)
        if v is not None:
            return v
    return default


class DataFormatter:
    """Formats sales data into structured text for LLM prompts."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        s = settings or get_settings()
        self._max_items = s.max_items_per_customer

    # ------------------------------------------------------------------
    # Customer analysis
    # ------------------------------------------------------------------

    def format_historical_context(self, customer_data: pd.DataFrame) -> str:
        if customer_data.empty:
            return "No historical data"

        df = customer_data
        if "PriorityScore" in df.columns:
            df = df.nlargest(self._max_items, "PriorityScore")
        else:
            df = df.head(self._max_items)

        lines = ["ItemCode | ItemName | Tier | Priority | Freq% | DaysLastBuy | CycleDays"]
        for _, r in df.iterrows():
            name = _sanitize(str(r.get("ItemName", "")))[:18]
            lines.append(
                f"{str(r.get('ItemCode', ''))[:8]} | {name:<18} | "
                f"{str(r.get('Tier', 'N/A'))[:3]} | {r.get('PriorityScore', 0):>8.1f} | "
                f"{r.get('FrequencyPercent', 0):>5.0f}% | "
                f"{int(r.get('DaysSinceLastPurchase', 0)):>11} | "
                f"{r.get('PurchaseCycleDays', 0):>9.1f}"
            )
        return "\n".join(lines)

    def format_current_visit(self, items: List[Dict[str, Any]]) -> str:
        if not items:
            return "No sales data"

        lines = ["ItemCode | ItemName | Actual | Recommended | Accuracy"]
        for item in items:
            code = str(_pick(item, "item_code", "itemCode", "ItemCode", default=""))
            name = _sanitize(str(_pick(item, "item_name", "itemName", "ItemName", default="")))[:20]
            actual = int(_pick(item, "actual_qty", "actualQuantity", "ActualQuantity", default=0))
            rec = int(_pick(item, "recommended_qty", "recommendedQuantity", "RecommendedQuantity", default=0))
            acc = (actual / rec * 100) if rec > 0 else 0
            lines.append(f"{code} | {name:<20} | {actual:>6} | {rec:>11} | {acc:.0f}%")

        t_act = sum(
            int(_pick(i, "actual_qty", "actualQuantity", "ActualQuantity", default=0))
            for i in items
        )
        t_rec = sum(
            int(_pick(i, "recommended_qty", "recommendedQuantity", "RecommendedQuantity", default=0))
            for i in items
        )
        t_acc = (t_act / max(t_rec, 1)) * 100
        lines.append(f"TOTAL | {'':20} | {t_act:>6} | {t_rec:>11} | {t_acc:.0f}%")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Route analysis
    # ------------------------------------------------------------------

    def format_route_performance(self, visited_customers: List[Dict[str, Any]]) -> str:
        if not visited_customers:
            return "No visited customer data"

        lines = ["Customer | Score | Items | Actual | Recommended | Accuracy"]
        for c in visited_customers:
            code = str(_pick(c, "customer_code", "customerCode", default=""))
            score = float(_pick(c, "score", "performanceScore", default=0) or 0)
            items = int(_pick(c, "item_count", "itemCount", "items_sold", default=0) or 0)
            actual = int(_pick(c, "total_actual", "totalActual", default=0) or 0)
            rec = int(_pick(c, "total_recommended", "totalRecommended", default=0) or 0)
            acc = (actual / rec * 100) if rec > 0 else 0
            lines.append(f"{code} | {score:>5.0f} | {items:>5} | {actual:>6} | {rec:>11} | {acc:.0f}%")
        return "\n".join(lines)
