"""Weekend-aware carry lookup for the van-load chain.

Single source of truth for "most recent reconciled leftover for a given
(route, item) before target_date". First non-skipped row wins (zeros are
honoured); empty input degrades to ``(0.0, 0.0, None)``.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

import pandas as pd


# Wire column names imported from the canonical rename map so producer-side
# column changes propagate here automatically (loud KeyError vs silent zeros).
# Lives in ``common/`` so this module doesn't reach back into a service module.
from common.wire_schemas import SALES_TRANSACTIONS_RENAME as _RENAME

_SX_WIRE: Dict[str, str] = {v: k for k, v in _RENAME.items()}  # snake -> PascalCase

_TRX_DATE_COL      = _SX_WIRE["trx_date"]
_ROUTE_COL         = _SX_WIRE["route_code"]
_ITEM_COL          = _SX_WIRE["item_code"]
_LEFTOVER_NEXT_COL = _SX_WIRE["leftover_to_next_day"]
_YAUMI_LEFTOVER_COL = _SX_WIRE["yaumi_leftover"]


def lookup_prior_leftover(
    sx_idx: Dict[Tuple[str, str, pd.Timestamp], Tuple[float, ...]],
    route_code: str,
    item_code: str,
    target_date: pd.Timestamp,
    *,
    lookback_days: int = 14,
    leftover_pos: int = 0,
) -> Tuple[float, Optional[pd.Timestamp]]:
    """Walk back day-by-day in ``sx_idx`` up to ``lookback_days``, return
    ``(leftover, source_date)`` for the most-recent row strictly before
    ``target_date``. Returns ``(0.0, None)`` if no row exists in-window.
    """
    if not sx_idx or lookback_days <= 0:
        return 0.0, None
    target_ts = pd.Timestamp(target_date).normalize()
    route_key = str(route_code)
    item_key = str(item_code)
    for back in range(1, int(lookback_days) + 1):
        prev_ts = target_ts - pd.Timedelta(days=back)
        row = sx_idx.get((route_key, item_key, prev_ts))
        if row is not None:
            return float(row[leftover_pos]), prev_ts
    return 0.0, None


def build_yesterday_leftover_map(
    sx_df: Optional[pd.DataFrame],
    route_codes: list[str],
    target_date: pd.Timestamp,
    *,
    lookback_days: int = 14,
) -> Dict[Tuple[str, str], Tuple[float, float, Optional[pd.Timestamp]]]:
    """Vectorised batch version returning
    ``{(route, item): (engine_leftover, rep_leftover, source_date)}`` for
    every pair with at least one row in the prior ``lookback_days`` window.
    Empty / missing input returns an empty map.
    """
    if sx_df is None or sx_df.empty:
        return {}
    needed = {_TRX_DATE_COL, _ROUTE_COL, _ITEM_COL, _LEFTOVER_NEXT_COL}
    if not needed.issubset(sx_df.columns):
        return {}

    target_ts = pd.Timestamp(target_date).normalize()
    window_start = target_ts - pd.Timedelta(days=int(lookback_days))

    df = sx_df.copy()
    df[_TRX_DATE_COL] = pd.to_datetime(df[_TRX_DATE_COL], errors="coerce").dt.normalize()
    df = df.dropna(subset=[_TRX_DATE_COL])
    # Restrict to lookback window AND the route set; < target_ts cutoff
    # matches the per-pair contract ("most recent row strictly before").
    df = df[
        (df[_TRX_DATE_COL] >= window_start)
        & (df[_TRX_DATE_COL] < target_ts)
        & (df[_ROUTE_COL].astype(str).isin([str(r) for r in route_codes]))
    ]
    if df.empty:
        return {}

    df[_ROUTE_COL] = df[_ROUTE_COL].astype(str).str.strip()
    df[_ITEM_COL]  = df[_ITEM_COL].astype(str).str.strip()
    df[_LEFTOVER_NEXT_COL] = pd.to_numeric(df[_LEFTOVER_NEXT_COL], errors="coerce").fillna(0.0)
    if _YAUMI_LEFTOVER_COL in df.columns:
        df[_YAUMI_LEFTOVER_COL] = pd.to_numeric(df[_YAUMI_LEFTOVER_COL], errors="coerce")
    else:
        # Rep-side column absent (older mirror snapshot) -- NaN leaves
        # yaumi_opening_stock alone downstream.
        df[_YAUMI_LEFTOVER_COL] = pd.NA

    # Latest trx_date per (route, item) -- skips non-trip gaps.
    idx = df.groupby([_ROUTE_COL, _ITEM_COL])[_TRX_DATE_COL].idxmax()
    latest = df.loc[idx]

    out: Dict[Tuple[str, str], Tuple[float, float, Optional[pd.Timestamp]]] = {}
    for _, r in latest.iterrows():
        engine_leftover = float(r[_LEFTOVER_NEXT_COL])
        rep_leftover_raw = r[_YAUMI_LEFTOVER_COL]
        rep_leftover = float(rep_leftover_raw) if pd.notna(rep_leftover_raw) else float("nan")
        src_date = pd.Timestamp(r[_TRX_DATE_COL])
        out[(str(r[_ROUTE_COL]), str(r[_ITEM_COL]))] = (engine_leftover, rep_leftover, src_date)
    return out


__all__ = ["lookup_prior_leftover", "build_yesterday_leftover_map"]
