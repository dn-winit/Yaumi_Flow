"""Helpers used by every service's ``config/settings.py``.

Currently a single canonical reader for the shared
``YF_ALLOW_ORIGINS`` env var so every FastAPI service interprets it
identically (comma / semicolon list, JSON array, or default to
``http://localhost:3000``). Five services previously duplicated this
function verbatim.
"""

from __future__ import annotations

import json
import os


def read_allow_origins() -> list[str]:
    """Read the shared ``YF_ALLOW_ORIGINS`` env var.

    Accepted forms:
      * ``http://a.com,http://b.com`` (comma or semicolon separated)
      * ``["http://a.com", "http://b.com"]`` (JSON array)
      * unset / empty -> ``["http://localhost:3000"]``
    """
    raw = os.getenv("YF_ALLOW_ORIGINS", "").strip()
    if not raw:
        return ["http://localhost:3000"]
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass
    return [s.strip() for s in raw.replace(";", ",").split(",") if s.strip()]
