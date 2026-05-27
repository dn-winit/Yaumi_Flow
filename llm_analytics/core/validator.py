"""
Response validation -- whitelists every entity-like token the LLM writes
against the set we put in the prompt. The model still hallucinates SKU or
customer codes occasionally even with strong "do not invent" instructions
in the system prompt; this is the defense-in-depth that guarantees no
fabricated code reaches the persisted column.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Iterable, List, Set

logger = logging.getLogger(__name__)


# Customer codes in this domain are plain numeric strings, 6-12 digits.
# Item codes look like ``50-XXXX`` (legacy) OR ``XXXXXXXXXX`` (10-digit
# numeric, e.g. 6003031209) OR ``Item-XXX`` (LLM-friendly form sometimes
# emitted). All three families are caught by these patterns.
_CUSTOMER_CODE_RE = re.compile(r"\b(\d{6,12})\b")
_ITEM_CODE_RES = (
    re.compile(r"\b(\d{2}-\d{3,5})\b"),         # 50-0731, 50-4408
    # Item-XXX form: model often appends alphanumeric suffixes (e.g. "Item-1025FG"
    # in the legacy example blocks). Allow [A-Za-z0-9]{3,12} after the dash so
    # the whole token is treated as one unit; the digits-only group is the
    # whitelist key, the trailing alpha (if any) is part of the same hallucination.
    re.compile(r"\bItem-([A-Za-z0-9]{3,12})\b"),
    re.compile(r"\b(60\d{8,10})\b"),             # 6003031209
)


def _scrub_codes(
    text: str,
    *,
    allowed_items: Set[str],
    allowed_customers: Set[str],
) -> str:
    """Replace any item/customer code in ``text`` that's not in the allow-sets.

    Customer codes are matched only after the legacy ``Customer-`` prefix
    pattern OR when they appear as standalone 6-12 digit tokens. Item codes
    are matched against all three known families.
    """
    # Drop legacy "Customer-NNN" prefix entirely; the bare digits below
    # handle whitelist enforcement after the prefix strip.
    text = re.sub(r"Customer-(\d+)", r"\1", text)

    def _check_customer(m: re.Match) -> str:
        code = m.group(1)
        return code if code in allowed_customers else "[unverified-customer]"

    text = _CUSTOMER_CODE_RE.sub(_check_customer, text)

    for pattern in _ITEM_CODE_RES:
        def _check_item(m: re.Match, _allowed=allowed_items) -> str:
            # group(0) is the full match (with prefix if any); group(1) is
            # the bare number. Whitelist against both forms so a model
            # writing "Item-1025" matches an input "1025".
            full = m.group(0)
            bare = m.group(1)
            if full in _allowed or bare in _allowed:
                return full
            return "[unverified-item]"

        text = pattern.sub(_check_item, text)
    return text


def scrub_hallucinated_entities(
    response: Dict[str, Any],
    *,
    allowed_items: Iterable[str] = (),
    allowed_customers: Iterable[str] = (),
    text_fields: Iterable[str] = (),
) -> Dict[str, Any]:
    """Walk every narrative field in ``response`` and whitelist codes.

    No-op for fields not in ``text_fields`` (lets callers be explicit
    about which artifact's schema to enforce). Returns the mutated
    response dict for chaining.
    """
    allowed_items_set = {str(x) for x in allowed_items}
    allowed_customers_set = {str(x) for x in allowed_customers}

    def _walk(v: Any) -> Any:
        if isinstance(v, str):
            return _scrub_codes(
                v,
                allowed_items=allowed_items_set,
                allowed_customers=allowed_customers_set,
            )
        if isinstance(v, list):
            return [_walk(x) for x in v]
        return v

    for key in text_fields:
        if key in response:
            response[key] = _walk(response[key])
    return response


# Back-compat alias kept for the route-analysis call site; new code should
# call ``scrub_hallucinated_entities`` directly with the full allow-set.
def sanitize_customer_codes(response: Dict[str, Any], actual_codes: Set[str]) -> Dict[str, Any]:
    """Legacy entry point -- delegates to scrub_hallucinated_entities."""
    return scrub_hallucinated_entities(
        response,
        allowed_customers=actual_codes,
        text_fields=(
            "route_summary",
            "supervisor_priorities",
            "high_performers_with_practices",
            "critical_issues",
        ),
    )


# Per-artifact text-field tuples -- single source of truth for which
# response keys carry free-form narrative that needs scrubbing.
CUSTOMER_ANALYSIS_TEXT_FIELDS = (
    "performance_summary",
    "supervisor_instructions",
    "strengths",
    "weaknesses",
)
ROUTE_ANALYSIS_TEXT_FIELDS = (
    "route_summary",
    "supervisor_priorities",
    "high_performers_with_practices",
    "critical_issues",
)
PRE_VISIT_BRIEFING_TEXT_FIELDS = (
    "briefing",
    "key_items",
    "heads_up",
)
