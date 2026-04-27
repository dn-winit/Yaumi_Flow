"""
Data formatters -- convert DataFrames and dicts into prompt-ready text tables.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from llm_analytics.config.settings import Settings, get_settings


def _sanitize(text: str) -> str:
    return str(text).replace('"', "inch").replace("'", "").replace("\n", " ").replace("\r", " ")


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
            code = str(item.get("itemCode", ""))
            name = _sanitize(str(item.get("itemName", "")))[:20]
            actual = int(item.get("actualQuantity", 0))
            rec = int(item.get("recommendedQuantity", 0))
            acc = (actual / rec * 100) if rec > 0 else 0
            lines.append(f"{code} | {name:<20} | {actual:>6} | {rec:>11} | {acc:.0f}%")

        t_act = sum(int(i.get("actualQuantity", 0)) for i in items)
        t_rec = sum(int(i.get("recommendedQuantity", 0)) for i in items)
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
            code = str(c.get("customerCode", ""))
            score = c.get("performanceScore", 0)
            items = c.get("itemCount", 0)
            actual = c.get("totalActual", 0)
            rec = c.get("totalRecommended", 0)
            acc = (actual / rec * 100) if rec > 0 else 0
            lines.append(f"{code} | {score:>5.0f} | {items:>5} | {actual:>6} | {rec:>11} | {acc:.0f}%")
        return "\n".join(lines)
