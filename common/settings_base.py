"""Helpers shared across every service's ``config/settings.py`` (single
canonical reader for ``YF_ALLOW_ORIGINS`` so all FastAPI services parse it
identically)."""

from __future__ import annotations

import json
import os
from pathlib import Path


def data_root(project_root: Path) -> Path:
    """Unified on-disk data root: ``YF_DATA_ROOT`` if set, else ``<project_root>/data``.

    Single canonical resolver so every service plants files under the same root.
    ``project_root`` is the caller's repo root (typically ``Path(__file__).resolve().parent.parent.parent``).
    """
    raw = os.getenv("YF_DATA_ROOT", "").strip()
    return Path(raw).resolve() if raw else project_root / "data"


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
