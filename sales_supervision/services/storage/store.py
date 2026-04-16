"""
File-based session storage -- save/load/list supervision sessions as JSON.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from sales_supervision.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class SessionStore:
    """Persists supervision sessions as JSON files."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._dir = Path((settings or get_settings()).storage_dir) / "sessions"
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _session_path(self, route_code: str, date: str) -> Path:
        return self._dir / f"session_{route_code}_{date}.json"

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self, session_data: Dict[str, Any]) -> Dict[str, Any]:
        route = session_data.get("routeCode", "")
        date = session_data.get("date", "")
        path = self._session_path(route, date)

        path.write_text(json.dumps(session_data, default=str, indent=2), encoding="utf-8")
        logger.info("Session saved: %s", path.name)

        return {"success": True, "path": str(path), "sessionId": session_data.get("sessionId", "")}

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load(self, route_code: str, date: str) -> Optional[Dict[str, Any]]:
        path = self._session_path(route_code, date)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.error("Failed to load session %s: %s", path.name, exc)
            return None

    # ------------------------------------------------------------------
    # Exists
    # ------------------------------------------------------------------

    def exists(self, route_code: str, date: str) -> bool:
        return self._session_path(route_code, date).exists()

    # ------------------------------------------------------------------
    # List
    # ------------------------------------------------------------------

    def list_dates(self, route_code: Optional[str] = None) -> List[str]:
        """Return sorted list of dates that have saved sessions."""
        dates = set()
        pattern = f"session_{route_code}_*.json" if route_code else "session_*.json"
        for f in self._dir.glob(pattern):
            parts = f.stem.split("_")
            if len(parts) >= 3:
                dates.add(parts[-1])  # date is last part
        return sorted(dates, reverse=True)

    def list_sessions(self, date: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return metadata for all saved sessions."""
        results = []
        for f in sorted(self._dir.glob("session_*.json")):
            parts = f.stem.split("_")
            if len(parts) < 3:
                continue
            route = parts[1]
            sess_date = parts[2]
            if date and sess_date != date:
                continue
            results.append({
                "routeCode": route,
                "date": sess_date,
                "filename": f.name,
                "sizeBytes": f.stat().st_size,
                "modified": f.stat().st_mtime,
            })
        return results

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete(self, route_code: str, date: str) -> bool:
        path = self._session_path(route_code, date)
        if path.exists():
            path.unlink()
            return True
        return False
