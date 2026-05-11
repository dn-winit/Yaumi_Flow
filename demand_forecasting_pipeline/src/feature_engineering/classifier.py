"""
ADI / CV^2 classification of demand-pattern per pair (Syntetos-Boylan 2005).

Each pair lands in one of four buckets:
    smooth        ADI < adi_thr  and  CV^2 <  cv2_thr
    intermittent  ADI >= adi_thr  and  CV^2 <  cv2_thr
    erratic       ADI < adi_thr  and  CV^2 >= cv2_thr
    lumpy         ADI >= adi_thr  and  CV^2 >= cv2_thr

Pairs with fewer non-zero observations than ``min_observations`` can't be
classified reliably - a single-point series trivially has CV^2=0 and would
be mislabelled "smooth". For those pairs :func:`classify_dataset` forces
``intermittent`` (the conservative bucket whose routing menu handles sparse
demand best).

ADI is ``inf`` for all-zero series by definition; the exported DataFrame
replaces non-finite ADI / CV^2 with ``None`` so the values serialize cleanly
through CSV -> FastAPI JSON without special-casing downstream.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

_INTERMITTENT = "intermittent"
_SMOOTH = "smooth"
_ERRATIC = "erratic"
_LUMPY = "lumpy"


def compute_adi_cv2(series) -> tuple[float, float]:
    """Return (ADI, CV^2) for a single pair's target series.

    Returns ``(inf, nan)`` for all-zero series and ``(nan, nan)`` for empty.
    Callers are expected to sanitise these at the artifact boundary via
    :func:`_finite_or_none`.
    """
    s = np.asarray(series, dtype=float)
    if s.size == 0:
        return np.nan, np.nan
    nz = s[s > 0]
    if nz.size == 0:
        return np.inf, np.nan
    adi = s.size / nz.size
    mean_nz = nz.mean()
    if mean_nz == 0:
        return adi, np.nan
    cv2 = (nz.std(ddof=0) / mean_nz) ** 2
    return float(adi), float(cv2)


def classify_pair(adi: float, cv2: float, adi_thr: float, cv2_thr: float) -> str:
    """Four-quadrant classification. Non-numeric / NaN inputs default to
    ``intermittent`` - the safest bucket when the signal is unreliable."""
    if adi is None or cv2 is None:
        return _INTERMITTENT
    try:
        if np.isnan(adi) or np.isnan(cv2):
            return _INTERMITTENT
    except TypeError:
        return _INTERMITTENT
    if adi < adi_thr and cv2 < cv2_thr:
        return _SMOOTH
    if adi >= adi_thr and cv2 < cv2_thr:
        return _INTERMITTENT
    if adi < adi_thr and cv2 >= cv2_thr:
        return _ERRATIC
    return _LUMPY


def _finite_or_none(v) -> float | None:
    """Drop ``inf`` / ``nan`` / ``None`` down to a single ``None`` sentinel
    that CSV, pandas and pydantic all round-trip cleanly."""
    if v is None:
        return None
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    return fv if np.isfinite(fv) else None


def classify_dataset(
    df: pd.DataFrame,
    group_keys: list[str],
    target_col: str,
    adi_thr: float,
    cv2_thr: float,
    *,
    min_observations: int = 3,
) -> pd.DataFrame:
    """One row per pair, indexed by ``group_keys``; columns ``adi``, ``cv2``,
    ``class``.

    Pairs with fewer than ``min_observations`` non-zero data points get
    ``class = "intermittent"`` regardless of their computed ADI / CV^2: those
    values aren't statistically meaningful on tiny samples. The raw ADI /
    CV^2 are still exported (as numbers or ``None``) so the artifact stays
    auditable - only the class assignment is forced conservative.
    """
    min_obs = int(min_observations)
    rows: list[list] = []
    for keys, g in df.groupby(group_keys):
        values = g[target_col].to_numpy(dtype=float)
        n_nonzero = int((values > 0).sum())
        adi, cv2 = compute_adi_cv2(values)

        cls = _INTERMITTENT if n_nonzero < min_obs else classify_pair(adi, cv2, adi_thr, cv2_thr)

        key_tuple = keys if isinstance(keys, tuple) else (keys,)
        rows.append([*key_tuple, _finite_or_none(adi), _finite_or_none(cv2), cls])

    return (
        pd.DataFrame(rows, columns=list(group_keys) + ["adi", "cv2", "class"])
        .set_index(group_keys)
    )
