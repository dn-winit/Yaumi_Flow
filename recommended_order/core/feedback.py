"""
Adaptive feedback loop (Sprint-4: closed-loop attribution).

The loop now has proper attribution. Instead of applying a single route-level
multiplier to every source (Sprint-3), we:

    1. Read all stored recommendation CSVs in the rolling window
       (``SafetyClamps.feedback_window_days``) -- every row carries the
       generator that produced it (``Source``) thanks to the Sprint-1 CSV
       schema persistence.
    2. Read every ``session_*.json`` saved in the same window from
       ``sales_supervision/data/sessions/``. Sessions hold the *actual* driver
       outcome: visited? sold? how many units?
    3. Inner-join on ``(route_code, date, customer_code, item_code)``. Rows
       where the customer wasn't visited are dropped (a no-show is a routing
       problem, not a recommendation failure).
    4. Detect adversarial supervisor sessions (reject-rate > Nsigma from the
       route's session-level distribution) and exclude them.
    5. For each (route, source) pair compute a **shrinkage estimator**
       (Empirical-Bayes flavour): the route's hit rate is blended with the
       corpus hit rate for that source using prior strength k derived from
       the corpus median sample count. Cold start -> multiplier = 1.0;
       strong signal -> multiplier mostly reflects route reality.
    6. EMA-smooth against the previous run's multipliers persisted in
       ``data/feedback_multipliers.json`` (thread-safe, atomic write).

All thresholds (k divisor, EMA alpha, multiplier range, adversarial zscore,
window bounds, bad-source floor) live in ``SafetyClamps``.

Cold-start safe: the file may not exist and every route may have zero
attributed samples -- everything collapses to multiplier 1.0, confidence 0.0.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from recommended_order.config.constants import SafetyClamps

logger = logging.getLogger(__name__)

# All sources we emit multipliers for. Any extras seen in data are carried
# transparently, but these three are always present (filled with 1.0 when
# no signal exists) so callers needn't special-case missing keys.
KNOWN_SOURCES = ("history", "peer", "basket", "reactivation", "seed")


# ===========================================================================
# Persistence of per-(route, source) multipliers across runs
# ===========================================================================

_PERSIST_LOCK = threading.Lock()


def _persist_path(shared_data_dir: str, clamps: SafetyClamps) -> Path:
    return Path(shared_data_dir) / clamps.feedback_multipliers_filename


def load_persisted_multipliers(
    shared_data_dir: str, clamps: SafetyClamps,
) -> Dict[str, Dict[str, Dict[str, float]]]:
    """Load ``feedback_multipliers.json``.

    Schema: ``{route: {source: {"multiplier": float, "n": int, "confidence": float}}}``.
    Missing/unreadable file -> empty dict (cold start).
    """
    path = _persist_path(shared_data_dir, clamps)
    if not path.exists():
        return {}
    try:
        with _PERSIST_LOCK:
            payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Could not read %s: %s -- starting cold", path.name, exc)
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def save_persisted_multipliers(
    data: Dict[str, Dict[str, Dict[str, float]]],
    shared_data_dir: str,
    clamps: SafetyClamps,
) -> None:
    """Atomic write: tmp file in same dir + rename. Thread-safe via a lock.
    File is tiny (KB) -- no rotation is needed."""
    path = _persist_path(shared_data_dir, clamps)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _PERSIST_LOCK:
        fd, tmp = tempfile.mkstemp(
            prefix=path.stem + ".", suffix=path.suffix, dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


# ===========================================================================
# Load: recommendations + sessions, together, for attribution
# ===========================================================================

def _iter_dates_in_window(today: datetime, window_days: int) -> List[str]:
    start = today.date() - timedelta(days=int(window_days))
    return [
        (start + timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(int(window_days) + 1)
    ]


def _load_recs_in_window(
    file_storage_dir: Path, window_days: int, today: datetime,
) -> pd.DataFrame:
    """Vectorised read of all per-(date, route) recommendation CSVs whose date
    falls in the trailing window."""
    if not file_storage_dir.exists():
        return pd.DataFrame()
    dates = set(_iter_dates_in_window(today, window_days))
    frames: List[pd.DataFrame] = []
    # File name pattern: recommendations_YYYY-MM-DD_<route>.csv
    for f in file_storage_dir.glob("recommendations_*_*.csv"):
        # Parse date out of the filename (no need to open the CSV to filter).
        stem = f.stem  # recommendations_2026-04-14_9105
        parts = stem.split("_")
        if len(parts) < 3:
            continue
        date_str = parts[1]
        if date_str not in dates:
            continue
        try:
            df = pd.read_csv(f, low_memory=False)
        except Exception as exc:
            logger.warning("Could not read %s: %s", f.name, exc)
            continue
        if df.empty:
            continue
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _load_sessions_in_window(
    sessions_dir: Path, window_days: int, today: datetime,
) -> pd.DataFrame:
    """Read every ``session_*.json`` whose ``date`` is within the window into
    a long frame of item-level outcomes.

    Schema fault tolerance (Sprint-4, edge case): session files with missing
    keys / wrong types are skipped with a warning -- one bad file must not
    crash the whole feedback pass.
    """
    cols = [
        "route_code", "date", "session_id",
        "customer_code", "item_code",
        "actual_qty", "was_sold", "was_edited",
        "visited",
    ]
    if not sessions_dir.exists():
        return pd.DataFrame(columns=cols)
    dates = set(_iter_dates_in_window(today, window_days))

    rows: List[Dict[str, Any]] = []
    for f in sessions_dir.glob("session_*.json"):
        try:
            session = json.loads(f.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Skipping malformed session %s: %s", f.name, exc)
            continue
        try:
            route = str(session.get("routeCode", "")).strip()
            date_str = str(session.get("date", "")).strip()
            sid = str(session.get("sessionId", f.stem))
            customers = session.get("customers") or {}
            if not route or not date_str or date_str not in dates:
                continue
            if not isinstance(customers, dict):
                logger.warning("Session %s has non-dict customers; skipping", f.name)
                continue
            for cust_code, cust in customers.items():
                if not isinstance(cust, dict):
                    continue
                visited = bool(cust.get("visited", False))
                items = cust.get("items") or []
                if not visited:
                    # Keep one sentinel row so we know this customer was
                    # planned but not visited -- ``was_visited==False`` rows
                    # are filtered out of attribution below.
                    rows.append({
                        "route_code": route, "date": date_str, "session_id": sid,
                        "customer_code": str(cust_code), "item_code": "",
                        "actual_qty": 0, "was_sold": False, "was_edited": False,
                        "visited": False,
                    })
                    continue
                if not isinstance(items, list):
                    continue
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    item_code = str(it.get("itemCode", "")).strip()
                    if not item_code:
                        continue
                    try:
                        act_qty = int(it.get("actualQuantity", 0) or 0)
                    except (TypeError, ValueError):
                        act_qty = 0
                    rows.append({
                        "route_code": route, "date": date_str, "session_id": sid,
                        "customer_code": str(cust_code), "item_code": item_code,
                        "actual_qty": act_qty,
                        "was_sold": bool(it.get("wasSold", act_qty > 0)),
                        "was_edited": bool(it.get("wasEdited", False)),
                        "visited": True,
                    })
        except Exception as exc:
            logger.warning("Skipping malformed session %s: %s", f.name, exc)
            continue
    if not rows:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(rows, columns=cols)


def _build_attribution(
    recs: pd.DataFrame, sessions: pd.DataFrame,
) -> pd.DataFrame:
    """Inner-join stored recs with session outcomes so every row carries:
    ``recommended_qty``, ``actual_qty`` (0 if not sold), ``source``,
    ``confidence``, ``was_visited``.

    Edge case (Sprint-4): item-code mismatch from type/whitespace quirks.
    ``.astype(str).str.strip()`` on both sides before the join -- relying on
    type discipline upstream is fragile.
    """
    if recs.empty:
        return pd.DataFrame()

    # Normalise keys on both sides.
    r = recs.rename(columns={
        "RouteCode": "route_code",
        "TrxDate": "date",
        "CustomerCode": "customer_code",
        "ItemCode": "item_code",
        "RecommendedQuantity": "recommended_qty",
        "Source": "source",
        "Confidence": "confidence",
    })
    keep_cols = [
        "route_code", "date", "customer_code", "item_code",
        "recommended_qty", "source", "confidence",
    ]
    for col in keep_cols:
        if col not in r.columns:
            r[col] = None
    r = r[keep_cols].copy()
    for k in ("route_code", "date", "customer_code", "item_code", "source"):
        r[k] = r[k].astype(str).str.strip()
    r["recommended_qty"] = pd.to_numeric(r["recommended_qty"], errors="coerce").fillna(0).astype(int)
    r["confidence"] = pd.to_numeric(r["confidence"], errors="coerce").fillna(0.0)

    if sessions.empty:
        # Brand-new / dormant routes: no sessions at all. Return an empty
        # attribution frame; downstream code treats this as cold-start.
        return pd.DataFrame()

    s = sessions.copy()
    for k in ("route_code", "date", "customer_code", "item_code", "session_id"):
        if k in s.columns:
            s[k] = s[k].astype(str).str.strip()

    # Which (route, date, customer) were visited?
    visited = (
        s.groupby(["route_code", "date", "customer_code"])["visited"]
        .max()
        .reset_index()
        .rename(columns={"visited": "was_visited"})
    )

    # Per-(route, date, customer, item) actuals -- sum in case an item
    # appears twice in the same session's items list.
    items = s[s["visited"] & (s["item_code"] != "")].groupby(
        ["route_code", "date", "customer_code", "item_code"], as_index=False
    ).agg(actual_qty=("actual_qty", "sum"),
          was_sold=("was_sold", "max"),
          session_id=("session_id", "first"))

    # Attribution = recs + visited flag + actuals.
    attr = r.merge(visited, on=["route_code", "date", "customer_code"], how="inner")
    attr = attr.merge(items,
                      on=["route_code", "date", "customer_code", "item_code"],
                      how="left")
    attr["actual_qty"] = attr["actual_qty"].fillna(0).astype(int)
    attr["was_sold"] = attr["was_sold"].fillna(False).astype(bool)
    attr["session_id"] = attr["session_id"].fillna("")

    # Drop no-show rows (routing failure, not a recommendation failure).
    attr = attr[attr["was_visited"].astype(bool)].copy()
    if attr.empty:
        return attr

    # Outcomes.
    attr["was_bought"] = attr["actual_qty"] > 0
    denom = np.maximum.reduce([
        attr["actual_qty"].astype(float).values,
        attr["recommended_qty"].astype(float).values,
        np.ones(len(attr)),
    ])
    err = np.abs(attr["recommended_qty"].astype(float).values
                 - attr["actual_qty"].astype(float).values) / denom
    attr["qty_accuracy"] = 1.0 - np.minimum(1.0, err)
    return attr


# ===========================================================================
# Adversarial-session defence
# ===========================================================================

def _filter_adversarial_sessions(
    attr: pd.DataFrame, clamps: SafetyClamps,
) -> pd.DataFrame:
    """Drop sessions whose per-route reject-rate is > Nsigma from the route's
    session-level distribution. Pure data-driven -- no hardcoded supervisor
    blacklist.

    Reject-rate per session = 1 - (items bought / items recommended) in that
    session. A route with only one session has nothing to compare against and
    is left alone.
    """
    if attr.empty:
        return attr
    sess = attr.groupby(["route_code", "session_id"]).agg(
        total=("recommended_qty", "size"),
        bought=("was_bought", "sum"),
    ).reset_index()
    sess["reject_rate"] = 1.0 - (sess["bought"] / sess["total"].clip(lower=1))

    drop_keys: List[Tuple[str, str]] = []
    for route, g in sess.groupby("route_code"):
        if len(g) < 3:
            continue  # not enough sessions to estimate distribution
        mean = float(g["reject_rate"].mean())
        std = float(g["reject_rate"].std(ddof=0))
        if std <= 0:
            continue
        z = (g["reject_rate"] - mean) / std
        bad = g[z.abs() > clamps.feedback_adversarial_zscore]
        for _, row in bad.iterrows():
            drop_keys.append((row["route_code"], row["session_id"]))
            logger.warning(
                "feedback_adversarial route=%s session=%s reject_rate=%.2f "
                "zscore=%.2f -> excluded from attribution",
                row["route_code"], row["session_id"],
                float(row["reject_rate"]), float((row["reject_rate"] - mean) / std),
            )
    if not drop_keys:
        return attr
    drop_df = pd.DataFrame(drop_keys, columns=["route_code", "session_id"])
    merged = attr.merge(drop_df.assign(_drop=True),
                        on=["route_code", "session_id"], how="left")
    return merged[merged["_drop"].isna()].drop(columns=["_drop"])


# ===========================================================================
# Shrinkage estimator
# ===========================================================================

def _shrinkage_multipliers(
    attr: pd.DataFrame,
    previous: Dict[str, Dict[str, Dict[str, float]]],
    clamps: SafetyClamps,
) -> Tuple[Dict[str, Dict[str, float]], Dict[str, Dict[str, float]],
           Dict[str, Dict[str, Dict[str, float]]]]:
    """Return (adjustments, confidence, persist_payload).

    * ``adjustments[route][source]`` -> float in [mult_min, mult_max].
    * ``confidence[route][source]``  -> float in [0, 1] (min(1, n/k)).
    * ``persist_payload``            -> JSON-serialisable dict for the file.
    """
    alpha = float(clamps.feedback_ema_alpha)
    mult_lo = float(clamps.feedback_multiplier_min)
    mult_hi = float(clamps.feedback_multiplier_max)

    adjustments: Dict[str, Dict[str, float]] = {}
    confidence: Dict[str, Dict[str, float]] = {}
    payload: Dict[str, Dict[str, Dict[str, float]]] = {}

    # Always carry previous entries forward for routes we see no new data
    # on (so a quiet week doesn't destroy the prior signal). They decay
    # toward 1.0 via EMA ONLY when a new observation lands.
    for route, per_source in previous.items():
        adjustments.setdefault(route, {})
        confidence.setdefault(route, {})
        payload.setdefault(route, {})
        for src, rec in per_source.items():
            adjustments[route][src] = float(rec.get("multiplier", 1.0))
            confidence[route][src] = float(rec.get("confidence", 0.0))
            payload[route][src] = {
                "multiplier": float(rec.get("multiplier", 1.0)),
                "confidence": float(rec.get("confidence", 0.0)),
                "n": int(rec.get("n", 0)),
            }

    if attr.empty:
        # Cold start (edge case: brand-new route / dormant-60-days route):
        # no attribution -> every source already defaults to multiplier=1.0
        # upstream. We still return the persisted payload unchanged.
        return adjustments, confidence, payload

    # Corpus-wide per-source stats (hit rate + qty acc + sample size).
    corp = attr.groupby("source").agg(
        n=("was_bought", "size"),
        hits=("was_bought", "sum"),
        qty_acc=("qty_accuracy", "mean"),
    )
    corp["hit_rate"] = corp["hits"] / corp["n"].clip(lower=1)
    corpus_hit: Dict[str, float] = {
        str(s): float(corp.at[s, "hit_rate"]) for s in corp.index
    }
    corpus_qty: Dict[str, float] = {
        str(s): float(corp.at[s, "qty_acc"]) for s in corp.index
    }

    # Bad-source warning (edge case: all-time weak source -- fix is to
    # disable, not to re-weight; out-of-scope for Sprint 4 but we flag it).
    for src, hr in corpus_hit.items():
        if hr < clamps.feedback_bad_source_floor:
            logger.warning(
                "feedback_weak_source source=%s corpus_hit_rate=%.3f floor=%.3f "
                "-> consider disabling this generator",
                src, hr, clamps.feedback_bad_source_floor,
            )

    # Per-route-per-source n for deriving prior strength k.
    per = attr.groupby(["route_code", "source"]).agg(
        n=("was_bought", "size"),
        hits=("was_bought", "sum"),
        qty_acc=("qty_accuracy", "mean"),
    ).reset_index()
    per["hit_rate"] = per["hits"] / per["n"].clip(lower=1)

    corpus_median_n = float(per["n"].median()) if len(per) else 0.0
    k = max(1.0, corpus_median_n / max(1.0, clamps.feedback_prior_strength_divisor))

    for _, row in per.iterrows():
        route = str(row["route_code"])
        src = str(row["source"])
        n = int(row["n"])
        route_hit = float(row["hit_rate"])
        corp_hr = corpus_hit.get(src, route_hit)
        if corp_hr <= 0:
            # Degenerate corpus -- can't compute a relative multiplier.
            raw_mult = 1.0
        else:
            shrunk = (n * route_hit + k * corp_hr) / (n + k)
            raw_mult = shrunk / corp_hr
        raw_mult = float(np.clip(raw_mult, mult_lo, mult_hi))

        prev_mult = float(previous.get(route, {}).get(src, {}).get("multiplier", 1.0))
        smoothed = alpha * raw_mult + (1.0 - alpha) * prev_mult
        smoothed = float(np.clip(smoothed, mult_lo, mult_hi))

        conf = float(min(1.0, n / max(1.0, k)))

        adjustments.setdefault(route, {})[src] = round(smoothed, 4)
        confidence.setdefault(route, {})[src] = round(conf, 4)
        payload.setdefault(route, {})[src] = {
            "multiplier": round(smoothed, 4),
            "confidence": round(conf, 4),
            "n": n,
        }

    return adjustments, confidence, payload


# ===========================================================================
# Public entrypoint
# ===========================================================================

def compute_feedback_adjustments(
    *,
    file_storage_dir: str,
    sessions_dir: str,
    shared_data_dir: str,
    clamps: SafetyClamps,
    today: Optional[datetime] = None,
) -> Tuple[Dict[str, Dict[str, float]], Dict[str, Dict[str, float]]]:
    """Full pipeline: read recs + sessions, attribute, filter adversarial,
    compute shrinkage multipliers, EMA-smooth against previous run, persist.

    Returns:
        ``(adjustments, confidence)`` -- both keyed by route then source.
        Empty dicts on cold start. Safe to call with no sessions / no recs.

    Honours ``SafetyClamps.feedback_enabled`` (caller usually checks too).
    Window is clamped to [feedback_window_min_days, feedback_window_max_days].
    """
    today_dt = today or datetime.utcnow()
    window = max(
        clamps.feedback_window_min_days,
        min(clamps.feedback_window_max_days, int(clamps.feedback_window_days)),
    )

    recs = _load_recs_in_window(Path(file_storage_dir), window, today_dt)
    sessions = _load_sessions_in_window(Path(sessions_dir), window, today_dt)
    attr = _build_attribution(recs, sessions)
    attr = _filter_adversarial_sessions(attr, clamps)

    previous = load_persisted_multipliers(shared_data_dir, clamps)
    adjustments, confidence, payload = _shrinkage_multipliers(
        attr, previous, clamps,
    )
    try:
        save_persisted_multipliers(payload, shared_data_dir, clamps)
    except Exception as exc:  # pragma: no cover -- non-critical
        logger.warning("Could not persist feedback multipliers: %s", exc)

    logger.info(
        "feedback_computed attributed_rows=%d routes_with_signal=%d "
        "sources_with_signal=%d window_days=%d",
        0 if attr is None else int(len(attr)),
        len(adjustments),
        sum(len(v) for v in adjustments.values()),
        window,
    )
    return adjustments, confidence


# ===========================================================================
# Score adjustment (applied at merge time in the engine)
# ===========================================================================

def apply_adjustments_to_candidates(
    candidates: List[Any],
    adjustments: Dict[str, float],
    *,
    confidence: Optional[Dict[str, float]] = None,
) -> List[Any]:
    """Multiply each candidate's ``priority_score`` by its source multiplier.

    When the multiplier != 1.0 we also append a ``feedback_adjusted`` signal
    to the candidate's explanation (the plain-English string lives in
    ``core/explain.py`` -- no string duplication here).

    Mutates the Candidate objects in place and returns them for chaining.
    """
    if not adjustments:
        return candidates
    # Local import avoids a circular dep at module load (explain <- generators
    # <- calibration -> feedback).
    from recommended_order.core.explain import (
        KIND_FEEDBACK_ADJUSTED, Signal, detail_feedback_adjusted,
    )
    for cand in candidates:
        src = getattr(cand, "source", "")
        mult = adjustments.get(src)
        if mult is None or abs(mult - 1.0) <= 1e-6:
            continue
        cand.priority_score = round(float(cand.priority_score) * float(mult), 2)
        conf = float((confidence or {}).get(src, 0.0))
        cand.signals = list(cand.signals) + [{
            "kind": KIND_FEEDBACK_ADJUSTED,
            "detail": detail_feedback_adjusted(float(mult), src, int(round(conf * 100))),
            "weight": round(min(1.0, abs(mult - 1.0)), 4),
            "evidence": {
                "multiplier": round(float(mult), 4),
                "source": src,
                "confidence": round(conf, 4),
            },
        }]
    return candidates
