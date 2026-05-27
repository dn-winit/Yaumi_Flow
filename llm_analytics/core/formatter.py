"""
Data formatters -- convert dicts into prompt-ready text tables.

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

from llm_analytics.config.settings import Settings, get_settings


def _sanitize(text: str) -> str:
    """Strip every character that could break the prompt template, JSON
    parsing, or smuggle instructions into the model's view.

    Upstream sources (customer names, item names) are user-controlled in
    YaumiLive and can in principle contain ``{role}`` placeholders that
    would crash .format(), curly braces that would prematurely close our
    schema-mention block, or instruction-injection prose. This is the
    boundary where they get neutralised. Order matters:

      * '"' -> ' inch'   (item names like 'Bread 8" 6PC' would break JSON)
      * "'" -> '         (apostrophes break some templates)
      * control chars    (\\r, \\n, \\t -> space; <0x20 dropped)
      * '{', '}'         (template + schema-mention protection)
      * '\\' -> '/'      (escape sequence smuggling)
    """
    if text is None:
        return ""
    s = str(text)
    s = s.replace('"', " inch").replace("'", "")
    # Collapse any control char (incl. CR/LF/TAB and form-feed) into a space.
    s = "".join(ch if ord(ch) >= 0x20 else " " for ch in s)
    # Neutralise template + JSON-structural metacharacters that user-supplied
    # strings could otherwise use to corrupt the prompt or break .format().
    s = s.replace("{", "(").replace("}", ")").replace("\\", "/")
    # Bound the length so a pathological 10KB item name can't blow the
    # token budget single-handedly.
    return s[:120]


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


# Canonical key-variant tuples for the per-item LLM payload. Every producer
# of a current_items entry may use any of the four naming conventions; the
# formatter and analyzer both read via these tuples so a new convention only
# needs to be added in one place. Order: snake_case (Python/back-end) ->
# short camelCase (webapp wire) -> long camelCase (legacy browser) ->
# PascalCase (rec-engine raw rows).
REC_QTY_KEYS = (
    "recommended_qty",
    "recommendedQty",
    "recommendedQuantity",
    "RecommendedQuantity",
)
ACT_QTY_KEYS = (
    "actual_qty",
    "actualQty",
    "actualQuantity",
    "ActualQuantity",
)
ITEM_CODE_KEYS = ("item_code", "itemCode", "ItemCode")
ITEM_NAME_KEYS = ("item_name", "itemName", "ItemName")
TIER_KEYS      = ("tier", "Tier")
FREQ_KEYS      = ("frequency_percent", "frequencyPercent", "FrequencyPercent")
DAYS_SINCE_KEYS = ("days_since_last_purchase", "daysSinceLastPurchase", "DaysSinceLastPurchase")
CYCLE_KEYS     = ("purchase_cycle_days", "purchaseCycleDays", "PurchaseCycleDays")


class DataFormatter:
    """Formats sales data into structured text for LLM prompts."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        s = settings or get_settings()
        self._max_items = s.max_items_per_customer
        self._max_customers = s.max_customers_per_route

    # ------------------------------------------------------------------
    # Customer analysis
    # ------------------------------------------------------------------

    def format_current_visit(self, items: List[Dict[str, Any]]) -> str:
        """Wide visit table -- carries the planning facts the supervisor prompt
        cites ("buys this every 7 days", "frequency 93%"). Earlier 5-col form
        forced the LLM to invent these numbers."""
        if not items:
            return "No sales data"

        # Bound the table size so a pathological 500-item visit cannot blow
        # the prompt token budget. Totals below are computed against ``items``
        # (the full list) so the LLM still sees correct sums even when rows
        # are truncated.
        truncated = max(0, len(items) - self._max_items)
        shown = items[: self._max_items]

        # Column headers carry the UNIT so the LLM cannot mis-read "LastBuy=12"
        # as 12 weeks (real model failure mode we observed: ambiguous LastBuy
        # column got interpreted as weeks instead of days).
        lines = [
            "ItemCode | ItemName             | Actual | Rec | Acc% | Tier | Freq% | DaysSinceLastBuy | CycleDays"
        ]
        for item in shown:
            code   = str(_pick(item, *ITEM_CODE_KEYS, default=""))
            name   = _sanitize(str(_pick(item, *ITEM_NAME_KEYS, default="")))[:20]
            actual = int(_pick(item, *ACT_QTY_KEYS, default=0) or 0)
            rec    = int(_pick(item, *REC_QTY_KEYS, default=0) or 0)
            acc    = (actual / rec * 100) if rec > 0 else 0
            tier   = str(_pick(item, *TIER_KEYS, default=""))[:4]
            freq   = float(_pick(item, *FREQ_KEYS, default=0) or 0)
            last   = int(_pick(item, *DAYS_SINCE_KEYS, default=0) or 0)
            cycle  = float(_pick(item, *CYCLE_KEYS, default=0) or 0)
            lines.append(
                f"{code} | {name:<20} | {actual:>6} | {rec:>3} | {acc:>4.0f} | {tier:<4} | "
                f"{freq:>5.0f} | {last:>16} | {cycle:>9.1f}"
            )

        t_act = sum(int(_pick(i, *ACT_QTY_KEYS, default=0) or 0) for i in items)
        t_rec = sum(int(_pick(i, *REC_QTY_KEYS, default=0) or 0) for i in items)
        t_acc = (t_act / max(t_rec, 1)) * 100
        if truncated:
            lines.append(f"... ({truncated} additional items truncated from display)")
        lines.append(
            f"TOTAL    | {'':20} | {t_act:>6} | {t_rec:>3} | {t_acc:>4.0f} | "
            f"{'':4} | {'':>5} | {'':>16} | {'':>9}"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Route analysis
    # ------------------------------------------------------------------

    def format_route_performance(self, visited_customers: List[Dict[str, Any]]) -> str:
        if not visited_customers:
            return "No visited customer data"

        # Bound prompt growth so a 200-customer route doesn't blow the token
        # budget; show the worst performers first (lowest score) so the LLM's
        # supervisor briefing focuses on rows that actually need attention.
        sorted_customers = sorted(
            visited_customers,
            key=lambda c: float(_pick(c, "score", "performanceScore", default=0) or 0),
        )
        truncated = max(0, len(sorted_customers) - self._max_customers)
        shown = sorted_customers[: self._max_customers]

        lines = ["Customer | Score | Items | Actual | Recommended | Accuracy"]
        for c in shown:
            code = str(_pick(c, "customer_code", "customerCode", default=""))
            score = float(_pick(c, "score", "performanceScore", default=0) or 0)
            items = int(_pick(c, "item_count", "itemCount", "items_sold", default=0) or 0)
            actual = int(_pick(c, "total_actual", "totalActual", default=0) or 0)
            rec = int(_pick(c, "total_recommended", "totalRecommended", default=0) or 0)
            acc = (actual / rec * 100) if rec > 0 else 0
            lines.append(f"{code} | {score:>5.0f} | {items:>5} | {actual:>6} | {rec:>11} | {acc:.0f}%")
        if truncated:
            lines.append(f"... ({truncated} additional customers truncated; lowest-scoring shown above)")
        return "\n".join(lines)
