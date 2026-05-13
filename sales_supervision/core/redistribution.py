"""
Redistribution view-shaper -- pure, deterministic.

One algorithm, three callers:

  * ``/session/visit``           -- live, post-process_visit.
  * ``/session/saved``           -- replay across visited customers.
  * ``/session/unplanned``       -- drop-in invoices.

All three converge on the same code path so the panel renders identical
bytes whether the view came from a live tap or a page reload. No
mutation, no DB access, no RNG. The shaper reads session state and
emits a ``RedistributionView``. Sorting is fully specified so two
equivalent session states always emit the same JSON.

The cumulative buffer ledger (:func:`compute_buffer_ledger`) walks the
session in route order and tracks, per (customer, item), the running
spare van-load state immediately before and after each visit. Each
per-customer shaper call consults the ledger so allocation decisions
account for every earlier visit's deposits and withdrawals. The running
state is NOT surfaced on the wire -- only the resulting recipient
entries are -- but the ledger still drives whether and how much each
entry carries.
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

# Wire-schema imports are deferred to call time inside the shaper.
# ``sales_supervision.api.schemas`` lives under the ``api`` package
# whose ``__init__.py`` pulls in ``app.create_app`` -- a chain that
# transitively imports ``core.session`` and therefore THIS module.
# A top-level import here would deadlock on first import; deferring
# avoids the cycle without giving up the strict ConfigDict(extra=
# 'forbid') wire contract.
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
    """Inverse of :func:`is_unplanned_customer`. An empty-items customer
    (rare legacy stub) is classified as planned so the shaper never
    silently treats an unknown row as a drop-in."""
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
    """Signed per-item contribution map for one customer.

    Positive: surplus deposited into the buffer (planned under-fulfill).
    Negative: excess-demand withdrawn from the buffer or downstream
    (planned over-fulfill, or any drop-in actual).

    Drop-in rows pinned onto an otherwise-planned customer are treated
    as excess-demand (no plan reservation to compare to). One canonical
    contribution rule -- the shaper and the ledger both call this so
    they can never drift.
    """
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
        # Use ``recommended_qty`` rather than ``effective_recommended``
        # so a downstream adjustment from an earlier visit doesn't get
        # re-counted as additional movement here. The supervisor reasons
        # about the ORIGINAL plan vs the ACTUAL delivery.
        rec = max(0, int(it.recommended_qty or 0))
        act = max(0, int(it.actual_qty or 0))
        diff = rec - act
        if diff != 0:
            out[it.item_code] = out.get(it.item_code, 0) + diff
    return out


def _compute_van_load_and_planned(
    session: Session,
) -> tuple[Dict[str, int], Dict[str, int]]:
    """Pre-compute ``van_load[i]`` and ``total_planned[i]`` once per call.

    ``van_load[i]`` is the truck load for item ``i``. Sourced from
    ``SessionItem.van_inventory_qty`` across planned rows; we take MAX
    to absorb any per-row drift (zeros from a partial join, lower values
    from a stale snapshot). Higher is the less-punitive interpretation
    for the customer being audited.

    ``total_planned[i]`` is the sum of ``recommended_qty`` across planned
    customers. Drop-ins are excluded; their reservations were never made.
    """
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
    """Planned downstream candidates for ``item_code``.

    Downstream means: tier != UNPLANNED, recommended_qty > 0, and
    either not-yet-visited (seq == 0) or later on the route than the
    source. Caller sorts the returned list by (visit_sequence, code).
    """
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


# ----------------------------------------------------------------------
# Cumulative buffer ledger
#
# As visits land in route order each one either deposits into or
# withdraws from a running per-item spare buffer. The ledger walks the
# session in (visit_sequence ASC, customer_code ASC) order and records,
# per (customer, item), the buffer state immediately before and after
# that visit. Pure: same session in, byte-identical ledger out.
# ----------------------------------------------------------------------


def _visited_walk_order(session: Session) -> List[SessionCustomer]:
    """Visited customers in canonical ledger order."""
    visited = [c for c in session.customers.values() if c.visited]
    visited.sort(key=lambda c: (int(c.visit_sequence or 0), c.customer_code))
    return visited


def compute_buffer_ledger(
    session: Session,
) -> Dict[Tuple[str, str], Tuple[int, int]]:
    """Walk visited customers in route order; return per-(cust,item) state.

    For each visited customer C and each item C interacted with, the
    returned map carries ``(buffer_before, buffer_after)`` -- the spare
    van-load state immediately before C consumed / deposited, and
    immediately after. Values are SIGNED here. The shaper uses these
    pre/post states to drive allocation; nothing about the buffer is
    surfaced on the wire.

    Pure function: no DB, no mutation of ``session``.
    """
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
    """Compute the redistribution view for one visited customer.

    Pure function -- no DB, no mutation of ``session``. Same input,
    byte-identical output.

    Buffer-aware semantics
    ----------------------
    For each item ``i`` on the truck the initial buffer is
    ``van_load[i] - total_planned[i]`` (can be negative when the truck
    is over-committed). When ``buffer_ledger`` is supplied the per-item
    pre-state reflects every earlier visit's contribution.

    Surplus case (planned source under-fulfilled an item):
      * buffer >= 0 -> surplus grows the buffer; no downstream entry.
      * buffer  < 0 -> downstream absorbs up to ``min(surplus, -buffer)``;
        leftover grows the buffer.

    Excess-demand case (planned over-bought OR drop-in):
      * ``min(excess, max(buffer, 0))`` is absorbed by the buffer.
      * The remainder encroaches on downstream allocations through
        ``_eligible_recipients`` capacity.

    Pre-computed ``van_load`` / ``total_planned`` / ``item_names`` can be
    passed in by the orchestrators so an N-customer replay walks the
    session once instead of N times. When omitted the shaper computes
    them locally (single-customer / unit-test path).
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
            # Total source surplus on this item (positive) -- used to
            # split into "distributed downstream" vs "kept on truck".
            # Only meaningful for the ADD direction; stays 0 for the
            # excess-demand / drop-in path.
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

            # ``keptOnTruck`` -- surplus units that didn't reach any
            # downstream recipient (ADD direction only; the REDUCE side
            # records downstream customers losing share, never a source
            # surplus). Computed as the residual of ``surplus_total``
            # minus everything distributed.
            if signed > 0:
                distributed = sum(int(e.quantity) for e in entries)
                kept_on_truck = max(0, surplus_total - distributed)
                # Identity assertion: distributed + kept must equal total
                # surplus. Surface a loud log warning if drift ever shows
                # up -- the shaper would be off-contract.
                if distributed + kept_on_truck != surplus_total:
                    logger.warning(
                        "redistribution identity drift: source=%s item=%s "
                        "surplus=%d distributed=%d kept=%d",
                        source_customer_code, item_code, surplus_total,
                        distributed, kept_on_truck,
                    )
            else:
                kept_on_truck = 0

            # Surface the group when at least one recipient line exists
            # or the source kept unsold stock on the van. Empty-empty
            # groups would render as no-op rows.
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
        # Safe default -- empty view. The wire contract is preserved
        # regardless of failure.
        logger.exception(
            "shape_redistribution_view failed for %s (drop_in=%s): %s",
            source_customer_code, is_drop_in, exc,
        )
        return RedistributionView()


def compute_redistributions_for_saved_visits(
    session: Session,
    visited_codes_in_order: List[str],
) -> Dict[str, "RedistributionView"]:
    """Replay redistribution for every planned-visited customer.

    A single cumulative buffer ledger + van/planned/name maps are
    computed once and threaded into every per-customer shaper call --
    O(session) total work, not O(visited * session).
    """
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
    """Shape the drop-in redistribution view for each drop-in customer.

    For each drop-in we materialise a synthetic ``SessionCustomer`` on
    a copy of the session so the shaper sees the drop-in's actuals as
    if they were a tier=UNPLANNED row, then runs the same allocation
    pass. The original session is not mutated.
    """
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
