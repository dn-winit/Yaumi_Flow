"""
Redistribution view-shaper -- pure, deterministic.

Single algorithm shared by /session/visit, /session/saved, and /session/unplanned
so the panel renders identical bytes regardless of caller. No mutation, no DB,
no RNG. ``compute_buffer_ledger`` walks the session in route order so each
shaper call accounts for every earlier visit's deposits and withdrawals.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from sales_supervision.core.constants import (
    TIER_UNPLANNED,
    is_unplanned_customer,
)
from sales_supervision.models.schemas import (
    Session,
    SessionCustomer,
    SessionItem,
)

# Wire-schema imports deferred inside the shaper to break the api -> core import cycle.
if TYPE_CHECKING:  # pragma: no cover -- import for type checkers only
    from sales_supervision.api.schemas import RedistributionView

logger = logging.getLogger(__name__)


# Movement direction taxonomy for ``RedistributionEntry.direction``.
#   ADD    -- source's surplus REDIRECTED TO this upcoming customer.
#   REDUCE -- upcoming customer's allocation REDUCED because the source
#             consumed stock reserved for them.
REDISTRIBUTION_DIRECTION_ADD    = "add"
REDISTRIBUTION_DIRECTION_REDUCE = "reduce"


# ----------------------------------------------------------------------
# Pure helpers shared by the shaper + the ledger walk.
# ----------------------------------------------------------------------


def _is_planned_customer(c: SessionCustomer) -> bool:
    """Inverse of is_unplanned_customer; empty-items stubs count as planned."""
    return not is_unplanned_customer(c)


def _item_name_lookup(session: Session) -> Dict[str, str]:
    """First-wins ``item_code -> item_name`` map across the session."""
    out: Dict[str, str] = {}
    for cust in session.customers.values():
        for it in cust.items:
            if it.item_code and it.item_code not in out and it.item_name:
                out[it.item_code] = it.item_name
    return out


def _customer_item_contributions(
    cust: SessionCustomer, *, is_drop_in: bool,
) -> Dict[str, int]:
    """Signed per-item contribution: positive = surplus into buffer, negative = excess demand."""
    out: Dict[str, int] = {}
    if is_drop_in:
        for it in cust.items:
            qty = max(0, int(it.actual_qty or 0))
            if qty > 0 and it.item_code:
                out[it.item_code] = out.get(it.item_code, 0) - qty
        return out
    for it in cust.items:
        if not it.item_code:
            continue
        if it.tier == TIER_UNPLANNED:
            qty = max(0, int(it.actual_qty or 0))
            if qty > 0:
                out[it.item_code] = out.get(it.item_code, 0) - qty
            continue
        # Use recommended_qty (original plan), not effective_recommended, to avoid double-counting earlier adjustments.
        rec = max(0, int(it.recommended_qty or 0))
        act = max(0, int(it.actual_qty or 0))
        diff = rec - act
        if diff != 0:
            out[it.item_code] = out.get(it.item_code, 0) + diff
    return out


def _compute_van_load_and_planned(
    session: Session,
) -> tuple[Dict[str, int], Dict[str, int]]:
    """Pre-compute van_load[i] (MAX across rows to absorb drift) and total_planned[i] for planned customers."""
    van_load: Dict[str, int] = {}
    total_planned: Dict[str, int] = {}
    for cust in session.customers.values():
        if not _is_planned_customer(cust):
            continue
        for it in cust.items:
            if not it.item_code:
                continue
            if it.tier == TIER_UNPLANNED:
                continue
            vl = max(0, int(it.van_inventory_qty or 0))
            if vl > van_load.get(it.item_code, 0):
                van_load[it.item_code] = vl
            rec = max(0, int(it.recommended_qty or 0))
            if rec > 0:
                total_planned[it.item_code] = (
                    total_planned.get(it.item_code, 0) + rec
                )
    return van_load, total_planned


def _eligible_recipients(
    session: Session,
    source: Optional[SessionCustomer],
    item_code: str,
) -> List[tuple[int, str, SessionCustomer, SessionItem]]:
    """Planned downstream candidates for item_code (tier != UNPLANNED, rec > 0, seq=0 or seq > source)."""
    src_seq = int(source.visit_sequence) if source is not None else 0
    src_code = source.customer_code if source is not None else ""
    out: List[tuple[int, str, SessionCustomer, SessionItem]] = []
    for code, cust in session.customers.items():
        if code == src_code:
            continue
        if not _is_planned_customer(cust):
            continue
        seq = int(cust.visit_sequence or 0)
        if seq != 0 and seq <= src_seq:
            continue
        for it in cust.items:
            if it.item_code != item_code:
                continue
            if int(it.recommended_qty or 0) <= 0:
                continue
            out.append((seq, code, cust, it))
            break
    return out


def _recipient_absorb_capacity(item: SessionItem) -> int:
    """Remaining shortfall the recipient can still absorb on plan."""
    rec = max(0, int(item.recommended_qty or 0))
    act = max(0, int(item.actual_qty or 0))
    return max(0, rec - act)


def _clean_name(name: Optional[str]) -> str:
    """Trim padded whitespace off DB-sourced customer names."""
    return (name or "").strip()


# Cumulative buffer ledger: walks session in (seq ASC, code ASC) tracking per-item spare van-load.


def _visited_walk_order(session: Session) -> List[SessionCustomer]:
    """Visited customers in canonical ledger order."""
    visited = [c for c in session.customers.values() if c.visited]
    visited.sort(key=lambda c: (int(c.visit_sequence or 0), c.customer_code))
    return visited


def compute_buffer_ledger(
    session: Session,
) -> Dict[Tuple[str, str], Tuple[int, int]]:
    """Per-(cust,item) signed (buffer_before, buffer_after) across visited customers in route order. Pure."""
    ledger: Dict[Tuple[str, str], Tuple[int, int]] = {}
    van_load, total_planned = _compute_van_load_and_planned(session)

    running: Dict[str, int] = {}
    all_item_codes: set[str] = set(van_load.keys()) | set(total_planned.keys())
    for cust in session.customers.values():
        for it in cust.items:
            if it.item_code:
                all_item_codes.add(it.item_code)
    for ic in all_item_codes:
        running[ic] = int(van_load.get(ic, 0)) - int(total_planned.get(ic, 0))

    for cust in _visited_walk_order(session):
        is_drop_in = is_unplanned_customer(cust)
        contributions = _customer_item_contributions(
            cust, is_drop_in=is_drop_in,
        )
        for item_code, signed in contributions.items():
            if not item_code:
                continue
            before = running.get(item_code, 0)
            after = before + int(signed)
            running[item_code] = after
            ledger[(cust.customer_code, item_code)] = (before, after)

    return ledger


# ----------------------------------------------------------------------
# Shaper
# ----------------------------------------------------------------------


def shape_redistribution_view(
    session: Session,
    source_customer_code: str,
    *,
    is_drop_in: bool,
    buffer_ledger: Optional[Dict[Tuple[str, str], Tuple[int, int]]] = None,
    van_load: Optional[Dict[str, int]] = None,
    total_planned: Optional[Dict[str, int]] = None,
    item_names: Optional[Dict[str, str]] = None,
) -> "RedistributionView":
    """Pure shaper -- emits one customer's RedistributionView.

    Initial buffer per item is ``van_load[i] - total_planned[i]``; when ``buffer_ledger``
    is supplied the per-item pre-state reflects all earlier visits. Surplus grows the
    buffer (or fills downstream shortfalls when buffer is negative); excess demand is
    absorbed by buffer first then encroaches on downstream. Optional pre-computed maps
    let orchestrators replay N customers with O(session) work.
    """
    from sales_supervision.api.schemas import (
        RedistributionEntry as WireRedistributionEntry,
        RedistributionGroup,
        RedistributionView,
    )

    try:
        source = session.customers.get(source_customer_code)
        movements = _customer_item_contributions(
            source, is_drop_in=is_drop_in,
        ) if source is not None else {}

        names = item_names if item_names is not None else _item_name_lookup(session)
        if van_load is None or total_planned is None:
            vl_map, tp_map = _compute_van_load_and_planned(session)
            van_load = van_load if van_load is not None else vl_map
            total_planned = total_planned if total_planned is not None else tp_map

        # Largest absolute movement first, ties broken by item_code.
        item_iter = sorted(
            movements.items(), key=lambda kv: (-abs(kv[1]), kv[0]),
        )

        groups: List[RedistributionGroup] = []

        for item_code, signed in item_iter:
            if signed == 0 or not item_code:
                continue
            vl = int(van_load.get(item_code, 0))
            tp = int(total_planned.get(item_code, 0))
            if (
                buffer_ledger is not None
                and (source_customer_code, item_code) in buffer_ledger
            ):
                buffer_before_signed, _buffer_after_signed = buffer_ledger[
                    (source_customer_code, item_code)
                ]
                buffer_i = int(buffer_before_signed)
            else:
                buffer_i = vl - tp

            entries: List[WireRedistributionEntry] = []
            # Splits into distributed downstream vs kept on truck; only meaningful for ADD direction.
            surplus_total = 0

            if signed > 0:
                # ---- SURPLUS case (planned source under-fulfilled) ----
                surplus = int(signed)
                surplus_total = surplus
                if buffer_i < 0:
                    to_downstream = min(surplus, -buffer_i)
                    if to_downstream > 0:
                        pool = _eligible_recipients(session, source, item_code)
                        pool.sort(key=lambda t: (t[0], t[1]))
                        remaining = to_downstream
                        for _seq, code, cust, it in pool:
                            if remaining <= 0:
                                break
                            cap = _recipient_absorb_capacity(it)
                            if cap <= 0:
                                continue
                            give = min(remaining, cap)
                            if give <= 0:
                                continue
                            remaining -= give
                            entries.append(WireRedistributionEntry(
                                to=code,
                                toName=_clean_name(cust.customer_name),
                                quantity=int(give),
                                direction=REDISTRIBUTION_DIRECTION_ADD,
                            ))
            else:
                # ---- EXCESS-DEMAND case (over-buy or drop-in) ----
                excess = -int(signed)
                avail_buffer = max(0, buffer_i)
                from_downstream = excess - min(excess, avail_buffer)
                if from_downstream > 0:
                    pool = _eligible_recipients(session, source, item_code)
                    pool.sort(key=lambda t: (t[0], t[1]))
                    remaining = from_downstream
                    for _seq, code, cust, it in pool:
                        if remaining <= 0:
                            break
                        cap = _recipient_absorb_capacity(it)
                        if cap <= 0:
                            continue
                        give = min(remaining, cap)
                        if give <= 0:
                            continue
                        remaining -= give
                        entries.append(WireRedistributionEntry(
                            to=code,
                            toName=_clean_name(cust.customer_name),
                            quantity=int(give),
                            direction=REDISTRIBUTION_DIRECTION_REDUCE,
                        ))

            # keptOnTruck = surplus not absorbed downstream (ADD direction only).
            if signed > 0:
                distributed = sum(int(e.quantity) for e in entries)
                kept_on_truck = max(0, surplus_total - distributed)
                # Identity: distributed + kept must equal surplus_total.
                if distributed + kept_on_truck != surplus_total:
                    logger.warning(
                        "redistribution identity drift: source=%s item=%s "
                        "surplus=%d distributed=%d kept=%d",
                        source_customer_code, item_code, surplus_total,
                        distributed, kept_on_truck,
                    )
            else:
                kept_on_truck = 0

            # Suppress empty-empty groups (no recipients and nothing kept).
            if entries or kept_on_truck > 0:
                entries.sort(key=lambda e: (-e.quantity, e.to))
                groups.append(RedistributionGroup(
                    itemCode=item_code,
                    itemName=names.get(item_code, ""),
                    entries=entries,
                    keptOnTruck=int(kept_on_truck),
                ))

        # Wire ordering: itemCode ASC for stable output.
        groups.sort(key=lambda g: g.itemCode)

        return RedistributionView(groups=groups)
    except Exception as exc:
        # Safe default -- empty view preserves wire contract.
        logger.exception(
            "shape_redistribution_view failed for %s (drop_in=%s): %s",
            source_customer_code, is_drop_in, exc,
        )
        return RedistributionView()


def compute_redistributions_for_saved_visits(
    session: Session,
    visited_codes_in_order: List[str],
) -> Dict[str, "RedistributionView"]:
    """Replay redistribution for every visited customer in O(session) by sharing the ledger/maps."""
    ledger = compute_buffer_ledger(session)
    van_load, total_planned = _compute_van_load_and_planned(session)
    names = _item_name_lookup(session)
    out: Dict[str, "RedistributionView"] = {}
    for code in visited_codes_in_order:
        out[code] = shape_redistribution_view(
            session, code,
            is_drop_in=False,
            buffer_ledger=ledger,
            van_load=van_load,
            total_planned=total_planned,
            item_names=names,
        )
    return out


def compute_redistribution_for_unplanned(
    session: Session,
    dropin_items_per_customer: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, "RedistributionView"]:
    """Shape redistribution per drop-in via a synthetic tier=UNPLANNED row on a session copy."""
    if not dropin_items_per_customer:
        return {}

    base_customers = dict(session.customers)
    name_idx = _item_name_lookup(session)

    next_seq = 0
    for cust in session.customers.values():
        if cust.visited:
            next_seq = max(next_seq, int(cust.visit_sequence or 0))

    synth_by_code: Dict[str, SessionCustomer] = {}
    for code, items in dropin_items_per_customer.items():
        ccode = str(code).strip()
        if not ccode:
            continue
        synth_items: List[SessionItem] = []
        for row in items or []:
            ic = str(row.get("item_code") or "").strip()
            if not ic:
                continue
            try:
                qty = int(row.get("qty") or 0)
            except (TypeError, ValueError):
                qty = 0
            if qty <= 0:
                continue
            synth_items.append(SessionItem(
                item_code=ic,
                item_name=name_idx.get(ic, ""),
                recommended_qty=0,
                actual_qty=qty,
                was_sold=True,
                tier=TIER_UNPLANNED,
            ))
        prior = base_customers.get(ccode)
        cname = prior.customer_name if prior is not None else ""
        next_seq += 1
        synth_by_code[ccode] = SessionCustomer(
            customer_code=ccode,
            customer_name=cname,
            items=synth_items,
            visited=True,
            visit_sequence=next_seq,
        )

    scratch_customers = dict(base_customers)
    for ccode, synth in synth_by_code.items():
        scratch_customers[ccode] = synth
    scratch_session = Session(
        session_id=session.session_id,
        route_code=session.route_code,
        date=session.date,
        customers=scratch_customers,
        status=session.status,
    )
    ledger = compute_buffer_ledger(scratch_session)
    van_load, total_planned = _compute_van_load_and_planned(scratch_session)
    names = _item_name_lookup(scratch_session)

    out: Dict[str, "RedistributionView"] = {}
    for ccode in synth_by_code:
        out[ccode] = shape_redistribution_view(
            scratch_session, ccode,
            is_drop_in=True,
            buffer_ledger=ledger,
            van_load=van_load,
            total_planned=total_planned,
            item_names=names,
        )
    return out
