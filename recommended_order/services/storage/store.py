"""
Recommendation store -- file-based canonical storage.

One CSV per (date, route) in ``file_storage_dir``. Reads and writes go through
this class only. DB replication is orthogonal and lives in ``DbPusher``; the
store does not know or care whether a DB copy exists.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from recommended_order.config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class RecommendationStore:
    """Per-(date, route) CSV store. Single source of truth for reads."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._s = settings or get_settings()
        self._dir = Path(self._s.file_storage_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _path(self, date: str, route_code: Optional[str] = None) -> Path:
        if route_code:
            return self._dir / f"recommendations_{date}_{route_code}.csv"
        return self._dir / f"recommendations_{date}.csv"

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self, df: pd.DataFrame, date: str, route_code: str) -> Dict[str, Any]:
        """Persist a route's recommendations for a date to CSV."""
        if df.empty:
            return {"success": False, "records_saved": 0}
        path = self._path(date, route_code)
        df.to_csv(path, index=False)
        logger.info("Saved %d recs to %s", len(df), path)
        return {"success": True, "records_saved": len(df), "path": str(path)}

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, date: str, route_code: Optional[str] = None) -> pd.DataFrame:
        """Return the recommendations for a date (optionally a single route).
        Returns an empty frame when nothing is stored yet."""
        if route_code:
            path = self._path(date, route_code)
            if not path.exists():
                return pd.DataFrame()
            return pd.read_csv(path, low_memory=False)

        files = sorted(self._dir.glob(f"recommendations_{date}_*.csv"))
        if not files:
            return pd.DataFrame()
        return pd.concat([pd.read_csv(f, low_memory=False) for f in files], ignore_index=True)

    # ------------------------------------------------------------------
    # Existence checks
    # ------------------------------------------------------------------

    def exists(self, date: str, route_code: Optional[str] = None) -> bool:
        if route_code:
            return self._path(date, route_code).exists()
        return bool(list(self._dir.glob(f"recommendations_{date}_*.csv")))

    def exists_batch(self, date: str, route_codes: List[str]) -> Dict[str, bool]:
        return {rc: self._path(date, rc).exists() for rc in route_codes}

    # ------------------------------------------------------------------
    # Info (used by the /info/{date} endpoint)
    # ------------------------------------------------------------------

    def generation_info(self, date: str) -> Dict[str, Any]:
        files = sorted(self._dir.glob(f"recommendations_{date}_*.csv"))
        if not files:
            return {
                "exists": False, "date": date,
                "total_records": 0, "routes_count": 0,
                "customers_count": 0, "items_count": 0,
                "generated_at": None, "generated_by": None,
            }

        frames = [pd.read_csv(f, low_memory=False) for f in files]
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

        total = int(len(df))
        customers = int(df["CustomerCode"].nunique()) if "CustomerCode" in df.columns else 0
        items = int(df["ItemCode"].nunique()) if "ItemCode" in df.columns else 0
        # newest mtime across the per-route files = "latest generated at"
        newest_mtime = max(f.stat().st_mtime for f in files)
        generated_at = pd.Timestamp.fromtimestamp(newest_mtime).isoformat()

        return {
            "exists": True, "date": date,
            "total_records": total,
            "routes_count": len(files),
            "customers_count": customers,
            "items_count": items,
            "generated_at": generated_at,
            "generated_by": "file",
        }
