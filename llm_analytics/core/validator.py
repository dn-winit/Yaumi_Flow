"""
Response validation -- sanitize hallucinated data, validate structure.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Set

logger = logging.getLogger(__name__)


def sanitize_customer_codes(response: Dict[str, Any], actual_codes: Set[str]) -> Dict[str, Any]:
    """Replace hallucinated customer codes with [customer] placeholder."""
    codes_str = {str(c) for c in actual_codes}

    def _clean(text: str) -> str:
        def _replace_prefixed(m: re.Match) -> str:
            code = m.group(1)
            return code if code in codes_str else "[customer]"

        return re.sub(r"Customer-(\d+)", _replace_prefixed, text)

    # Mirror the ``RouteAnalysis`` schema -- those are the only fields the
    # LLM can drop a hallucinated customer code into. Listing keys that
    # the schema doesn't define would just be dead.
    text_keys = (
        "route_summary",
        "supervisor_priorities",
        "high_performers_with_practices",
        "critical_issues",
    )
    for key in text_keys:
        val = response.get(key)
        if isinstance(val, str):
            response[key] = _clean(val)
        elif isinstance(val, list):
            response[key] = [_clean(item) if isinstance(item, str) else item for item in val]

    return response
