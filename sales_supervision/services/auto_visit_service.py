"""Auto-visit reconciler -- mirrors live YaumiLive invoices into yf_supervision_*.

Removes the browser dependency: per tick, each configured route fetches today's
invoiced customers, diffs against the DB, then upserts via the same DbSaver path
used by /session/visit. Idempotent + crash-safe; failed mid-route ticks leave
partial state and the next tick re-evaluates.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

# Phase concurrency caps live in Settings; see config/settings.py.
from common.numeric import safe_float
from sales_supervision.config.settings import Settings, get_settings
from sales_supervision.core.constants import TIER_UNPLANNED
from sales_supervision.core.session import SessionManager
from sales_supervision.models.schemas import (
    ScoreResult,
    Session,
    SessionCustomer,
    SessionItem,
)
from sales_supervision.services.db_saver import DbSaver
from sales_supervision.services.live_actuals import LiveActualsClient
from sales_supervision.services.recommended_order_client import RecommendedOrderClient

logger = logging.getLogger(__name__)


def _session_item_from_rec(rec: dict) -> SessionItem:
    """Build a SessionItem from one yf_recommended_orders row.

    Single source of truth for the rec -> SessionItem mapping so the "new
    customer" branch and the "missing item on existing customer" branch in
    _merge_planned_customers can't drift.
    """
    from sales_supervision.models.schemas import SessionItem
    return SessionItem(
        item_code=str(rec.get("ItemCode", "")),
        item_name=str(rec.get("ItemName", "")),
        recommended_qty=int(rec.get("RecommendedQuantity", 0) or 0),
        tier=str(rec.get("Tier", "")),
        priority_score=safe_float(rec.get("PriorityScore")),
        days_since_last_purchase=int(rec.get("DaysSinceLastPurchase", 0) or 0),
        purchase_cycle_days=safe_float(rec.get("PurchaseCycleDays")),
        frequency_percent=safe_float(rec.get("FrequencyPercent")),
        van_inventory_qty=int(rec.get("VanLoad", 0) or 0),
        raw=rec,
    )


@dataclass
class TickReport:
    """Per-route outcome of one reconciliation tick."""

    route_code: str
    date: str
    planned_visits: int = 0
    unplanned_visits: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_code": self.route_code,
            "date": self.date,
            "planned_visits": self.planned_visits,
            "unplanned_visits": self.unplanned_visits,
            "error": self.error,
        }


@dataclass
class _SessionCacheEntry:
    session: Session
    # Last route-header signature persisted by the cron; None forces re-stamp on restart.
    last_header_signature: tuple[Any, ...] | None = None
    # Wall-clock of the last tick; drives TTL eviction so _sessions stays bounded.
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Serialises in-memory mutation across the worker pool (process_visit, _register_unplanned).
    lock: threading.Lock = field(default_factory=threading.Lock)


class AutoVisitService:
    """Walks configured routes once per tick and reconciles DB to YaumiLive."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        session_manager: SessionManager,
        live_actuals: LiveActualsClient,
        recommended_order: RecommendedOrderClient,
        db_saver: DbSaver,
    ) -> None:
        self._s = settings or get_settings()
        self._sessions: dict[str, _SessionCacheEntry] = {}  # session_id -> entry
        # Reentrant lock so a tick's resolve -> upsert chain can re-enter without deadlocking.
        self._sessions_lock = threading.RLock()
        self._mgr = session_manager
        self._live = live_actuals
        self._recs = recommended_order
        self._db = db_saver
        # Read by /health for data freshness; atomic reference assignment in CPython.
        self._last_reconcile_at: datetime | None = None

    @property
    def last_reconcile_at(self) -> datetime | None:
        return self._last_reconcile_at

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def reconcile_all(self) -> list[dict[str, Any]]:
        """Run one full reconciliation pass across every configured route.

        Date is computed in the configured business timezone (default Asia/Dubai)
        rather than server-local. Without this, a UTC host serving a Dubai
        operation would reconcile against "yesterday" for the four hours each
        day where UTC is still on the previous date while the UI is on today.
        """
        tz = ZoneInfo(self._s.auto_visit_timezone)
        date = datetime.now(tz).date().strftime("%Y-%m-%d")
        # Heartbeat at tick start so /health flips out of reconcile_stale immediately.
        self._last_reconcile_at = datetime.now(UTC)
        # TTL-evict idle (route, date) cache entries each tick.
        self._evict_stale_sessions()
        out: list[dict[str, Any]] = []
        for route in self._configured_routes():
            try:
                rep = self.reconcile_route(route, date)
            except Exception as exc:
                logger.exception("Auto-visit tick failed for %s: %s", route, exc)
                rep = TickReport(route_code=route, date=date, error=str(exc))
            out.append(rep.to_dict())
            # Per-route heartbeat keeps lag below the 2x-poll health threshold during long ticks.
            self._last_reconcile_at = datetime.now(UTC)
        if any(r.get("planned_visits") or r.get("unplanned_visits") for r in out):
            logger.info("Auto-visit tick summary: %s", out)
        else:
            # Heartbeat for no-op ticks so the cron is provably alive in the log.
            skipped = sum(1 for r in out if r.get("error"))
            logger.info(
                "Auto-visit tick heartbeat: %d routes processed, %d skipped",
                len(out), skipped,
            )
        return out

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _route_header_signature(snapshot: dict[str, Any]) -> tuple[Any, ...]:
        """Stable tuple over fields persisted by _upsert_route_header; equal tuples skip the MERGE.

        IMPORTANT: keep in sync with the UPDATE column set in DbSaver._upsert_route_header.
        """
        return (
            snapshot.get("plannedCustomers"),           # customers_planned
            snapshot.get("plannedVisitedCustomers"),    # customers_visited
            snapshot.get("totalRecommended"),           # planned_qty_recommended
            snapshot.get("visitedRecommended"),         # visited_qty_recommended
            snapshot.get("visitedActual"),              # visited_qty_actual
            snapshot.get("visitedAchievement"),         # route_performance_score
            snapshot.get("unplannedVisitedCustomers"),
            snapshot.get("remainingCustomers"),
            snapshot.get("status"),
        )

    def _evict_stale_sessions(self) -> None:
        """Drop _sessions entries older than auto_visit_session_ttl_seconds."""
        ttl = int(self._s.auto_visit_session_ttl_seconds)
        if ttl <= 0:
            return
        cutoff = datetime.now(UTC) - timedelta(seconds=ttl)
        with self._sessions_lock:
            stale = [
                sid for sid, cache in self._sessions.items()
                if cache.last_seen_at < cutoff
            ]
            for sid in stale:
                self._sessions.pop(sid, None)
        if stale:
            logger.info(
                "Auto-visit session cache eviction: dropped %d stale entries "
                "(ttl=%ds)", len(stale), ttl,
            )

    def reconcile_route(
        self,
        route_code: str,
        date: str,
    ) -> TickReport:
        """Reconcile a single (route, date) -- data sync only.

        Phase 0: refresh route header from session state (idle-tick skip).
        Phase 1: parallel data sync (live YaumiLive actuals -> DB upsert).

        LLM phases are gone -- analyses are generated on-demand by the webapp
        directly against llm_analytics; the cron no longer fires them.
        """
        report = TickReport(route_code=str(route_code), date=str(date))

        # 1. Resolve session (in-memory + DB-aware).
        session = self._resolve_session(route_code, date)
        if session is None:
            report.error = "no recommendations available; cannot scope a session"
            # WARNING so an empty route is greppable without polluting INFO.
            logger.warning(
                "Auto-visit skip route=%s date=%s -- no recommendations yet "
                "(retrying next tick; check recommended_order data load if "
                "this persists)",
                route_code, date,
            )
            return report

        # Phase 0: refresh route header from session state; signature guard skips no-op writes on idle routes.
        snapshot = session.to_dict()
        sig = self._route_header_signature(snapshot)
        with self._sessions_lock:
            cache_for_sig = self._sessions.get(session.session_id)
            prior_sig = cache_for_sig.last_header_signature if cache_for_sig else None
        if sig != prior_sig:
            self._db.refresh_route_header(snapshot)
            with self._sessions_lock:
                cache_for_sig = self._sessions.get(session.session_id)
                if cache_for_sig is not None:
                    cache_for_sig.last_header_signature = sig
                    cache_for_sig.last_seen_at = datetime.now(UTC)

        # ---- Phase 1: Data sync (parallel, cap=auto_visit_data_phase_workers) ----
        invoiced = self._live.get_route_sales(route_code, date) or []
        if invoiced:
            report.planned_visits, report.unplanned_visits = (
                self._sync_visits_parallel(session, invoiced)
            )
            # Bust the load_session_visits cache for this (route, date) so /saved reads fresh DB state.
            if report.planned_visits or report.unplanned_visits:
                self._db.invalidate_saved_visits(route_code, date)

        return report

    # ------------------------------------------------------------------
    # Phase 1 -- parallel data sync
    # ------------------------------------------------------------------

    def _sync_visits_parallel(
        self,
        session: Session,
        invoiced: list[dict[str, Any]],
    ) -> tuple[int, int]:
        """Upsert every invoiced customer in parallel so returns/voids propagate within one tick.

        process_visit / upsert_visit are idempotent on (route, date, customer);
        _register_unplanned preserves visit_sequence on re-touch. Returns (planned, unplanned).
        """
        with self._sessions_lock:
            cache = self._sessions[session.session_id]
        lock = cache.lock

        targets: list[tuple[str, str, dict[str, int]]] = []
        for visitor in invoiced:
            code = str(visitor.get("customer_code") or "").strip()
            if not code:
                continue
            cust_name = str(visitor.get("customer_name") or "").strip()
            actuals: dict[str, int] = {}
            for it in visitor.get("items") or []:
                ic = str(it.get("item_code") or "").strip()
                if not ic:
                    continue
                qty = int(it.get("qty") or 0)
                if qty <= 0:
                    continue
                actuals[ic] = qty
            targets.append((code, cust_name, actuals))

        if not targets:
            return 0, 0

        def _process(target: tuple[str, str, dict[str, int]]) -> str:
            code, cust_name, actuals = target
            # Mutate the in-memory session under ``lock`` (short critical
            # section) and snapshot it; release the lock BEFORE the DB write
            # so other workers actually run in parallel on their own customer.
            # Previously the entire DB upsert was inside ``with lock`` and the
            # ThreadPoolExecutor delivered zero real parallelism.
            with lock:
                if code in session.customers and any(
                    it.recommended_qty > 0 for it in session.customers[code].items
                ):
                    kind = "planned"
                    self._mgr.process_visit(session, code, actuals)
                    # Refresh off-plan items in place so supervision_items byte-tracks YaumiLive.
                    cust = session.customers[code]
                    cust.items = [it for it in cust.items if it.recommended_qty > 0]
                    planned_codes = {it.item_code for it in cust.items}
                    for ic, qty in actuals.items():
                        if ic in planned_codes:
                            continue
                        cust.items.append(SessionItem(
                            item_code=str(ic),
                            item_name="",
                            recommended_qty=0,
                            actual_qty=int(qty),
                            was_sold=True,
                            tier=TIER_UNPLANNED,
                            raw={
                                "ItemCode": str(ic),
                                "ItemName": "",
                                "RecommendedQuantity": 0,
                                "Tier": TIER_UNPLANNED,
                            },
                        ))
                else:
                    kind = "unplanned"
                    self._register_unplanned(session, code, cust_name, actuals)
                # Deep-copy via to_dict() to detach the snapshot from the
                # live session; concurrent mutations on other customers must
                # not race against the upsert reader below.
                snapshot = session.to_dict()
            # DB write happens outside the lock so true N-way parallelism is
            # restored across workers writing different (route, date, customer)
            # MERGE targets.
            self._db.upsert_visit(snapshot, code)
            return kind

        planned = unplanned = 0
        with ThreadPoolExecutor(
            max_workers=self._s.auto_visit_data_phase_workers,
            thread_name_prefix=f"av-data-{session.route_code}",
        ) as pool:
            for kind in pool.map(_process, targets):
                if kind == "planned":
                    planned += 1
                else:
                    unplanned += 1
        return planned, unplanned

    def invalidate_route(self, route_code: str, date: str) -> None:
        """Drop cached session for (route_code, date); next reconcile rebuilds via DB hydration."""
        with self._sessions_lock:
            stale = [
                sid for sid, cache in self._sessions.items()
                if cache.session.route_code == str(route_code)
                and cache.session.date == str(date)
            ]
            for sid in stale:
                self._sessions.pop(sid, None)

    def invalidate_all_for_date(self, date: str) -> int:
        """Drop every cached session matching ``date``; returns count evicted.

        Used by the demand_forecasting retrain cascade: after fresh recs land
        in yf_recommended_orders, every cached supervision session for today
        is scoring against stale recommendations. The next reconcile tick
        re-hydrates from DB and picks up the new plan.
        """
        target = str(date)
        with self._sessions_lock:
            stale = [
                sid for sid, cache in self._sessions.items()
                if cache.session.date == target
            ]
            for sid in stale:
                self._sessions.pop(sid, None)
        return len(stale)

    # ------------------------------------------------------------------
    # Session resolution
    # ------------------------------------------------------------------

    def _resolve_session(self, route_code: str, date: str) -> Session | None:
        """Return a session for (route, date) across ticks and restarts.

        Order: in-process cache, then DB rebuild (keeps original session_id for LLM idempotency),
        then fresh creation from recommended_order.
        """
        # 1. In-process cache; stamp last_seen_at so the TTL sweep can't drop active entries.
        now = datetime.now(UTC)
        with self._sessions_lock:
            for cache in self._sessions.values():
                if cache.session.route_code == str(route_code) and cache.session.date == str(date):
                    cache.last_seen_at = now
                    return cache.session

        # 2. Existing DB session for this (route, date).
        existing = self._db.load_session(str(route_code), str(date))
        if existing:
            session = self._mgr.rebuild_session(existing)
            recs = self._recs.get_recommendations(route_code, date) or []
            self._merge_planned_customers(session, recs)
            with self._sessions_lock:
                self._sessions[session.session_id] = _SessionCacheEntry(session=session)
            return session

        # 3. Fresh creation.
        recs = self._recs.get_recommendations(route_code, date)
        if not recs:
            return None
        session = self._mgr.create_session(str(route_code), str(date), recs)
        with self._sessions_lock:
            self._sessions[session.session_id] = _SessionCacheEntry(session=session)
        return session

    @staticmethod
    def _merge_planned_customers(session: Session, recs: list) -> None:
        """Merge recs into a (possibly DB-rebuilt) session, healing zero-rec items.

        Two modes per customer:
          * NEW customer (not in session.customers): create the customer + append
            every rec row as a SessionItem.
          * EXISTING customer (already in session.customers): for each rec, find
            the matching SessionItem by item_code. If found AND its recommended_qty
            is zero (typical for a session created BEFORE the rec cron fired),
            OVERWRITE the rec/planning fields from the incoming rec. If not found,
            append it. This is the self-healing path that closes the rec-timing
            race -- a session born with empty recs is repaired the next time
            this method runs against a non-empty rec list.

        Item-name + tier are ALSO overwritten when the in-memory value is empty,
        because the same race produces empty item names (no master-lookup at
        visit time when the customer came in via the unplanned branch).

        Snapshots pre-existing codes BEFORE the loop so each new customer
        accumulates all N rec rows (a naive in-place check would drop items
        2..N from a NEW customer being built up across multiple recs).
        """
        from sales_supervision.models.schemas import SessionCustomer
        pre_existing: set[str] = set(session.customers.keys())
        for rec in recs:
            ccode = str(rec.get("CustomerCode", ""))
            if not ccode:
                continue
            cust = session.customers.get(ccode)

            if ccode not in pre_existing:
                # NEW customer: build from scratch (one customer, many rec rows).
                if cust is None:
                    cust = SessionCustomer(
                        customer_code=ccode,
                        customer_name=str(rec.get("CustomerName", "")),
                    )
                    session.customers[ccode] = cust
                cust.items.append(_session_item_from_rec(rec))
                continue

            # EXISTING customer: heal zero/empty fields, leave non-empty
            # in-memory values alone (they represent rep actuals or manual edits).
            if cust is None:
                continue  # shouldn't happen given pre_existing membership

            # Heal blank customer_name from the rec.
            if not (cust.customer_name and cust.customer_name.strip()):
                cust.customer_name = str(rec.get("CustomerName", "")) or cust.customer_name

            icode = str(rec.get("ItemCode", ""))
            if not icode:
                continue
            rec_qty = int(rec.get("RecommendedQuantity", 0) or 0)

            matched = next((it for it in cust.items if it.item_code == icode), None)
            if matched is None:
                # Item missing from in-memory cust.items: append from rec.
                cust.items.append(_session_item_from_rec(rec))
                continue

            # Heal: if in-memory rec is zero but incoming is non-zero, OVERWRITE.
            if matched.recommended_qty == 0 and rec_qty > 0:
                matched.recommended_qty = rec_qty
                matched.tier = str(rec.get("Tier", "")) or matched.tier
                matched.priority_score = safe_float(rec.get("PriorityScore")) or matched.priority_score
                matched.days_since_last_purchase = (
                    int(rec.get("DaysSinceLastPurchase", 0) or 0) or matched.days_since_last_purchase
                )
                matched.purchase_cycle_days = (
                    safe_float(rec.get("PurchaseCycleDays")) or matched.purchase_cycle_days
                )
                matched.frequency_percent = (
                    safe_float(rec.get("FrequencyPercent")) or matched.frequency_percent
                )
                matched.raw = rec
            # Heal blank item_name regardless of rec_qty -- empty names are useless to readers.
            if not matched.item_name:
                matched.item_name = str(rec.get("ItemName", "")) or matched.item_name

    def _register_unplanned(
        self,
        session: Session,
        customer_code: str,
        customer_name: str,
        actual_sales: dict[str, int],
    ) -> None:
        """Register/refresh a drop-in customer; preserves the original visit_sequence on re-touch."""
        items = [
            SessionItem(
                item_code=str(ic), item_name="",
                recommended_qty=0, actual_qty=int(qty), was_sold=int(qty) > 0,
                tier=TIER_UNPLANNED,
                raw={
                    "ItemCode": str(ic),
                    "ItemName": "",
                    "RecommendedQuantity": 0,
                    "Tier": TIER_UNPLANNED,
                },
            )
            for ic, qty in actual_sales.items() if int(qty) > 0
        ]
        existing = session.customers.get(customer_code)
        seq = (
            existing.visit_sequence if existing and existing.visit_sequence > 0
            else session.visit_sequence_counter + 1
        )
        session.customers[customer_code] = SessionCustomer(
            customer_code=customer_code,
            customer_name=customer_name,
            items=items,
            visited=True,
            visit_sequence=seq,
            # No baseline to score against; coverage/accuracy/score = 0.
            score=ScoreResult(score=0.0, coverage=0.0, accuracy=0.0),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------


    def _configured_routes(self) -> list[str]:
        """Routes to monitor per tick; falls back to demand-forecasting's live_route_codes."""
        explicit = list(self._s.auto_visit_route_codes or [])
        if explicit:
            return [str(r).strip() for r in explicit if str(r).strip()]
        try:
            from demand_forecasting_pipeline.config.settings import get_settings as df_settings
            return [str(r).strip() for r in (df_settings().live_route_codes or []) if str(r).strip()]
        except Exception:
            return []
