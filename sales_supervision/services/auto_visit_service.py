"""Auto-visit reconciler -- mirrors live invoices into yf_supervision_*.

The browser-driven path (UI mounts -> polls live actuals -> fires
``/session/visit``) only writes rows while a supervisor has the route
page open. This reconciler removes that dependency: every poll cycle
it walks each configured route, fetches today's invoiced customers
from YaumiLive (via data_import), diffs against existing DB rows,
and upserts the missing visits.

End-to-end flow per (route, date):

  1. Get-or-create the in-memory session for ``route, date``.
     Recommendations are pulled from ``recommended_order`` so the
     session is scored against the same baseline the supervisor sees.

  2. Fetch invoiced customers from YaumiLive (planned + unplanned).

  3. Skip customers already persisted (idempotent across ticks).

  4. For each new customer:
       - Planned   -> standard ``SessionManager.process_visit`` path
                      (full coverage / accuracy / score).
       - Unplanned -> register an ad-hoc ``SessionCustomer`` from the
                      live actuals (no recommended baseline) so the
                      DB row captures the activity even though it's
                      out of plan.

  5. Trigger LLM analyses (per-customer briefing+retro and, when the
     planned set is fully visited, the route-level retro). Optional
     -- gated by ``llm_analytics_url``; absent means we just skip.

  6. Upsert each customer through the same ``DbSaver.upsert_visit``
     used by the manual path -- one persist surface, no drift.

The reconciler is idempotent and crash-safe: a tick that fails
mid-route logs and leaves whatever it had completed in the DB. The
next tick re-evaluates from scratch.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date as _date_cls, datetime
from typing import Any, Dict, List, Optional, Set, Tuple

# Phase concurrency caps live in Settings (auto_visit_data_phase_workers,
# auto_visit_llm_phase_workers) so ops can tune them per environment
# without a code change. See config/settings.py for the bounds.

from sales_supervision.config.settings import Settings, get_settings
from sales_supervision.core.constants import TIER_UNPLANNED, is_unplanned_customer
from sales_supervision.core.session import SessionManager
from sales_supervision.models.schemas import (
    ScoreResult,
    Session,
    SessionCustomer,
    SessionItem,
)
from sales_supervision.services.db_saver import DbSaver
from sales_supervision.services.live_actuals import LiveActualsClient
from sales_supervision.services.llm_client import LlmClient
from sales_supervision.services.recommended_order_client import RecommendedOrderClient

logger = logging.getLogger(__name__)


@dataclass
class TickReport:
    """Per-route outcome of one reconciliation tick."""

    route_code: str
    date: str
    planned_visits: int = 0
    unplanned_visits: int = 0
    llm_briefing_calls: int = 0
    llm_customer_calls: int = 0
    llm_route_calls: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "route_code": self.route_code,
            "date": self.date,
            "planned_visits": self.planned_visits,
            "unplanned_visits": self.unplanned_visits,
            "llm_briefing_calls": self.llm_briefing_calls,
            "llm_customer_calls": self.llm_customer_calls,
            "llm_route_calls": self.llm_route_calls,
            "error": self.error,
        }


@dataclass
class _SessionCacheEntry:
    session: Session
    # Visit count at the most recent route-LLM fire. The end-of-day
    # route summary refreshes whenever new visits have landed since
    # the last fire -- so by the final tick of the day, the DB column
    # holds the most complete state. ``0`` means "never fired in this
    # process" (cross-restart fires once before catching up).
    last_visit_count_at_route_llm: int = 0
    # Per-session lock serialising in-memory mutation across the worker
    # pool. process_visit / _register_unplanned mutate session.customers
    # and assign visit_sequence by scanning the current max -- two
    # threads without a lock would race on the sequence. Held only for
    # the in-memory mutation + snapshot capture (microseconds); released
    # before the slow HTTP / DB work each worker does.
    lock: threading.Lock = field(default_factory=threading.Lock)


class AutoVisitService:
    """Walks configured routes once per tick and reconciles DB to YaumiLive."""

    def __init__(
        self,
        *,
        settings: Optional[Settings] = None,
        session_manager: SessionManager,
        live_actuals: LiveActualsClient,
        recommended_order: RecommendedOrderClient,
        db_saver: DbSaver,
        llm_client: LlmClient,
    ) -> None:
        self._s = settings or get_settings()
        self._sessions: Dict[str, _SessionCacheEntry] = {}  # session_id -> entry
        self._mgr = session_manager
        self._live = live_actuals
        self._recs = recommended_order
        self._db = db_saver
        self._llm = llm_client
        # Last successful reconcile_all completion. Read by /health to
        # expose data freshness; reference assignment is atomic in CPython.
        self._last_reconcile_at: Optional[datetime] = None

    @property
    def last_reconcile_at(self) -> Optional[datetime]:
        return self._last_reconcile_at

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def reconcile_all(self) -> List[Dict[str, Any]]:
        """Run one full reconciliation pass across every configured route."""
        date = _date_cls.today().strftime("%Y-%m-%d")
        out: List[Dict[str, Any]] = []
        for route in self._configured_routes():
            try:
                rep = self.reconcile_route(route, date)
            except Exception as exc:
                logger.exception("Auto-visit tick failed for %s: %s", route, exc)
                rep = TickReport(route_code=route, date=date, error=str(exc))
            out.append(rep.to_dict())
        if any(r.get("planned_visits") or r.get("unplanned_visits") for r in out):
            logger.info("Auto-visit tick summary: %s", out)
        else:
            # Heartbeat for ticks where nothing landed -- still proves
            # the cron is alive and surfaces silent skips. Without this,
            # a fully-skipped tick was invisible in the logs and an
            # operator had no way to tell whether the scheduler was
            # running at all.
            skipped = sum(1 for r in out if r.get("error"))
            logger.info(
                "Auto-visit tick heartbeat: %d routes processed, %d skipped",
                len(out), skipped,
            )
        # Stamp completion so /health can report freshness even when
        # individual route reconciles errored (per-route failures are
        # already logged above; the tick itself completed).
        self._last_reconcile_at = datetime.utcnow()
        return out

    def reconcile_route(
        self,
        route_code: str,
        date: str,
        *,
        skip_llm: bool = False,
    ) -> TickReport:
        """Reconcile a single (route, date) pair. Caller catches and logs.

        Runs in three decoupled phases so a slow LLM call never blocks
        the DB sync supervisors are watching:

          Phase 1 -- Data sync. Walks new YaumiLive invoices and
                     upserts visit rows in parallel (cap from
                     ``auto_visit_data_phase_workers``). HTTP + DB
                     only; no LLM. Catches up the ``Visited X/Y``
                     tile in seconds.
          Phase 2 -- LLM analyses. Pre-visit briefings and customer
                     retros fire in a smaller pool (cap from
                     ``auto_visit_llm_phase_workers``) because LLM
                     providers rate-limit. Idempotent against the DB
                     columns so a tick that ran phase 2 can re-run
                     cheaply.
          Phase 3 -- Route-level retro. Single shot, refreshes when
                     new visits have landed since the last fire.

        ``skip_llm`` short-circuits phases 2 and 3 -- used by the
        synchronous /session/initialize hook so the page snaps to the
        correct ``visited`` count in seconds, not after every per-
        customer LLM call. The 60s cron picks up the LLM phases on the
        next tick (idempotent), so nothing is lost.
        """
        report = TickReport(route_code=str(route_code), date=str(date))

        # 1. Resolve session (in-memory + DB-aware).
        session = self._resolve_session(route_code, date)
        if session is None:
            report.error = "no recommendations available; cannot scope a session"
            # Visible heartbeat so a route stuck on an empty-cache miss
            # doesn't go silently dark for a whole shift. Logged at
            # WARNING so a routinely-empty route (genuinely no plan
            # today) is greppable but doesn't pollute INFO with noise.
            logger.warning(
                "Auto-visit skip route=%s date=%s -- no recommendations yet "
                "(retrying next tick; check recommended_order data load if "
                "this persists)",
                route_code, date,
            )
            return report

        # ---- Phase 0: Stamp the route header from current session state.
        # Cheap (one UPDATE-or-INSERT). Guarantees customers_planned /
        # customers_visited / visited_qty_* always reflect the latest
        # formula, even on a fully-idempotent tick where Phases 1/2/3
        # all short-circuit. Without this, a route that completed
        # earlier in the day keeps stale counts forever.
        self._db.refresh_route_header(session.to_dict())

        # ---- Phase 1: Data sync (parallel, cap=auto_visit_data_phase_workers) ----
        invoiced = self._live.get_route_sales(route_code, date) or []
        if invoiced:
            report.planned_visits, report.unplanned_visits = (
                self._sync_visits_parallel(session, invoiced)
            )

        if skip_llm:
            return report

        # ---- Phase 2: LLM analyses (parallel, cap=auto_visit_llm_phase_workers) ----
        if self._llm.configured and self._s.auto_visit_llm_enabled:
            report.llm_briefing_calls = self._fire_pending_briefings_parallel(session)
            report.llm_customer_calls = self._fire_customer_llms_parallel(session)

        # ---- Phase 3: Route-level retro (single shot) ----
        # Refreshes whenever new visits have landed since the last fire,
        # so the LATEST tick of the day always wins and the DB column
        # holds the end-of-day state by close of business.
        cache = self._sessions[session.session_id]
        current_visit_count = sum(
            1 for c in session.customers.values() if c.visited
        )
        if (self._llm.configured
                and self._s.auto_visit_llm_enabled
                and current_visit_count > 0
                and current_visit_count > cache.last_visit_count_at_route_llm):
            self._fire_route_llm(session)
            cache.last_visit_count_at_route_llm = current_visit_count
            report.llm_route_calls += 1

        return report

    # ------------------------------------------------------------------
    # Phase 1 -- parallel data sync
    # ------------------------------------------------------------------

    def _sync_visits_parallel(
        self,
        session: Session,
        invoiced: List[Dict[str, Any]],
    ) -> Tuple[int, int]:
        """Walk YaumiLive invoices, upsert each customer's visit + items
        in parallel. Every invoiced customer is re-processed on every
        tick so the DB tracks YaumiLive's current state -- returns,
        voids, and late-arriving line items all propagate within one
        cron cadence. ``process_visit`` and ``upsert_visit`` are
        idempotent on (route, date, customer); ``_register_unplanned``
        preserves the original ``visit_sequence`` on re-touch so visit
        ordering stays stable across ticks.

        Returns ``(planned_count, unplanned_count)``.
        """
        cache = self._sessions[session.session_id]
        lock = cache.lock

        targets: List[Tuple[str, str, Dict[str, int]]] = []
        for visitor in invoiced:
            code = str(visitor.get("customer_code") or "").strip()
            if not code:
                continue
            cust_name = str(visitor.get("customer_name") or "").strip()
            actuals: Dict[str, int] = {}
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

        def _process(target: Tuple[str, str, Dict[str, int]]) -> str:
            code, cust_name, actuals = target
            with lock:
                if code in session.customers and any(
                    it.recommended_qty > 0 for it in session.customers[code].items
                ):
                    kind = "planned"
                    self._mgr.process_visit(session, code, actuals)
                    # Refresh off-plan items in place: drop the previous
                    # set, re-derive from the current actuals. Planned
                    # items keep their rec_qty + the actuals process_visit
                    # just wrote. ``yf_supervision_items`` then byte-tracks
                    # YaumiLive line by line.
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
                snapshot = session.to_dict()
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

    # ------------------------------------------------------------------
    # Phase 2 -- parallel LLM analyses
    # ------------------------------------------------------------------

    def _fire_pending_briefings_parallel(self, session: Session) -> int:
        """Pre-visit briefings for every planned customer whose
        ``llm_pre_visit_briefing`` column is still NULL. Parallel,
        cap=auto_visit_llm_phase_workers. Idempotent against the DB column."""
        already_briefed = self._db.list_customers_with_briefing(
            session.route_code, session.date,
        )
        snapshot = session.to_dict()
        targets: List[str] = []
        for code, cust in session.customers.items():
            if code in already_briefed:
                continue
            if not cust.items or is_unplanned_customer(cust):
                continue
            targets.append(code)
        if not targets:
            return 0

        def _fire(code: str) -> int:
            return self._fire_briefing_one(session, code, snapshot=snapshot)

        with ThreadPoolExecutor(
            max_workers=self._s.auto_visit_llm_phase_workers,
            thread_name_prefix=f"av-llm-brief-{session.route_code}",
        ) as pool:
            return sum(pool.map(_fire, targets))

    def _fire_briefing_one(
        self,
        session: Session,
        customer_code: str,
        *,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Fire one pre-visit briefing and persist it.

        Shared primitive used by both the parallel cron path and the
        per-/visit fire path. ``snapshot`` is passed by the cron (one
        snapshot per tick, reused across all customers); the /visit
        path leaves it ``None`` and we snapshot here. Returns 1 if the
        briefing landed, 0 otherwise.

        ``save_pre_visit_briefing`` is always called, even when the LLM
        returned None -- the shared save path upserts the route header
        and the customer row first, then conditionally writes the
        briefing column. An LLM failure still leaves a customer row in
        place (column NULL, ready for retry) and route-header counts
        always match customer-table cardinality.
        """
        cust = session.customers.get(customer_code)
        if cust is None:
            return 0
        items_payload = [
            {
                "item_code": it.item_code,
                "item_name": it.item_name,
                "recommended_qty": int(it.recommended_qty),
                "tier": it.tier,
                "purchase_cycle_days": float(it.purchase_cycle_days or 0.0),
                "days_since_last_purchase": int(it.days_since_last_purchase or 0),
                "frequency_percent": float(it.frequency_percent or 0.0),
            }
            for it in cust.items
        ]
        briefing = self._llm.pre_visit_briefing(
            customer_code=customer_code,
            customer_name=cust.customer_name,
            route_code=session.route_code,
            date=session.date,
            items=items_payload,
        )
        snap = snapshot if snapshot is not None else session.to_dict()
        self._db.save_pre_visit_briefing(snap, customer_code, briefing)
        return 1 if briefing else 0

    def _fire_customer_llms_parallel(self, session: Session) -> int:
        """Customer-level retro analyses for every visited customer
        whose ``llm_performance_analysis`` column is still NULL.
        Parallel, cap=auto_visit_llm_phase_workers. Subsumes the previous tick's
        catch-up loop -- one source of truth for "which customers need
        an analysis"."""
        already_analysed = self._db.list_customers_with_performance_analysis(
            session.route_code, session.date,
        )
        targets = [
            code for code, cust in session.customers.items()
            if cust.visited and code not in already_analysed
        ]
        if not targets:
            return 0

        def _fire(code: str) -> int:
            self._fire_customer_llm(session, code)
            return 1

        with ThreadPoolExecutor(
            max_workers=self._s.auto_visit_llm_phase_workers,
            thread_name_prefix=f"av-llm-cust-{session.route_code}",
        ) as pool:
            return sum(pool.map(_fire, targets))

    def fire_llms_for_visit(
        self, session: Session, customer_code: str,
    ) -> None:
        """Fire briefing + customer LLM for one newly-visited customer.

        Called from /session/visit's BackgroundTask after the visit
        upsert lands so the green-dot moment populates the LLM columns
        within seconds instead of waiting up to 60s for the cron tick.
        Both fires are idempotent against the DB columns -- a column
        already populated short-circuits the LLM call. Route analysis
        stays on the cron's per-tick rhythm so a single route summary
        covers a burst of visits instead of firing per-visit.
        """
        if not self._llm.configured or not self._s.auto_visit_llm_enabled:
            return
        cust = session.customers.get(customer_code)
        if cust is None:
            return

        if cust.items and not is_unplanned_customer(cust):
            already_briefed = self._db.list_customers_with_briefing(
                session.route_code, session.date,
            )
            if customer_code not in already_briefed:
                self._fire_briefing_one(session, customer_code)

        if cust.visited:
            already_analysed = self._db.list_customers_with_performance_analysis(
                session.route_code, session.date,
            )
            if customer_code not in already_analysed:
                self._fire_customer_llm(session, customer_code)

    def invalidate_route(self, route_code: str, date: str) -> None:
        """Drop any cached session for ``(route_code, date)``.

        Called by /session/initialize so the next reconcile rebuilds
        from the current journey plan + DB state. Without this, a
        customer added to today's plan after the cron's session was
        cached would never appear as inplan -- the cache returns the
        stale session unchanged. Pre-existing visit + LLM rows survive
        because the next ``_resolve_session`` falls through to the DB
        rebuild branch, which calls ``hydrate_saved_visits`` to restore
        them.
        """
        stale = [
            sid for sid, cache in self._sessions.items()
            if cache.session.route_code == str(route_code)
            and cache.session.date == str(date)
        ]
        for sid in stale:
            self._sessions.pop(sid, None)

    # ------------------------------------------------------------------
    # Session resolution
    # ------------------------------------------------------------------

    def _resolve_session(self, route_code: str, date: str) -> Optional[Session]:
        """Return a session for (route, date), reusing across ticks AND
        across process restarts.

        Resolution order:
          1. In-process cache -- the running scheduler reuses the same
             ``Session`` object across consecutive ticks.
          2. DB lookup -- after a restart, the cache is empty; we
             rebuild the session from the most-recent header row in
             ``yf_supervision_routes`` so the existing ``session_id``
             is reused, NOT replaced. This keeps the LLM idempotency
             contract (already-briefed customers stay skipped).
          3. Fresh creation -- only when no DB row exists. Pulls
             recommendations from recommended_order to scope the
             planned customer set.
        """
        # 1. In-process cache.
        for cache in self._sessions.values():
            if cache.session.route_code == str(route_code) and cache.session.date == str(date):
                return cache.session

        # 2. Existing DB session for this (route, date).
        existing = self._db.load_session(str(route_code), str(date))
        if existing:
            session = self._mgr.rebuild_session(existing)
            recs = self._recs.get_recommendations(route_code, date) or []
            self._merge_planned_customers(session, recs)
            self._sessions[session.session_id] = _SessionCacheEntry(session=session)
            return session

        # 3. Fresh creation.
        recs = self._recs.get_recommendations(route_code, date)
        if not recs:
            return None
        session = self._mgr.create_session(str(route_code), str(date), recs)
        self._sessions[session.session_id] = _SessionCacheEntry(session=session)
        return session

    @staticmethod
    def _merge_planned_customers(session: Session, recs: list) -> None:
        """Add planned customers from ``recs`` to a rebuilt session
        when they're not already present. Existing customers (rebuilt
        from DB rows OR previously merged this tick) keep their state;
        only customers absent from the rebuild get added.

        Recommendation rows are row-per-(customer, item); a customer
        with N recommended items shows up as N rows. We snapshot the
        set of pre-existing customer codes BEFORE the loop so each new
        customer accumulates ALL their item rows. A naive
        ``ccode in session.customers`` check after each insert would
        treat the second row for a newly-added customer as "already
        merged" and silently drop items 2..N -- the bug this guards
        against.
        """
        from sales_supervision.models.schemas import SessionCustomer, SessionItem
        pre_existing: Set[str] = set(session.customers.keys())
        for rec in recs:
            ccode = str(rec.get("CustomerCode", ""))
            if not ccode or ccode in pre_existing:
                continue
            cust = session.customers.get(ccode)
            if cust is None:
                cust = SessionCustomer(
                    customer_code=ccode,
                    customer_name=str(rec.get("CustomerName", "")),
                )
                session.customers[ccode] = cust
            cust.items.append(SessionItem(
                item_code=str(rec.get("ItemCode", "")),
                item_name=str(rec.get("ItemName", "")),
                recommended_qty=int(rec.get("RecommendedQuantity", 0)),
                tier=str(rec.get("Tier", "")),
                priority_score=float(rec.get("PriorityScore", 0)),
                days_since_last_purchase=int(rec.get("DaysSinceLastPurchase", 0)),
                purchase_cycle_days=float(rec.get("PurchaseCycleDays", 0)),
                frequency_percent=float(rec.get("FrequencyPercent", 0)),
                van_inventory_qty=int(rec.get("VanLoad", 0)),
                raw=rec,
            ))

    def _register_unplanned(
        self,
        session: Session,
        customer_code: str,
        customer_name: str,
        actual_sales: Dict[str, int],
    ) -> None:
        """Register (or refresh) a drop-in customer's invoiced items.

        Re-callable: on first touch the customer is assigned the next
        visit_sequence; on subsequent ticks the original sequence is
        preserved so visit ordering stays stable while items rebuild
        from current YaumiLive state.
        """
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
            # No baseline to score against -- coverage/accuracy/score = 0.
            score=ScoreResult(score=0.0, coverage=0.0, accuracy=0.0),
        )

    # ------------------------------------------------------------------
    # LLM triggers (in-flow pipeline)
    #
    # Three steps, each fires automatically as the operational state
    # advances -- no manual intervention required:
    #
    #   1. Pre-visit briefing  -- after session scoping, before any
    #                              visit. One per planned customer.
    #   2. Customer analysis   -- after a visit lands (we have actuals).
    #                              One per visited customer.
    #   3. Route analysis      -- after every planned customer in the
    #                              route is visited. One per route.
    #
    # Each step is idempotent against the DB: if the column is already
    # populated, the LLM is skipped on the next tick.
    # ------------------------------------------------------------------

    def _fire_customer_llm(self, session: Session, customer_code: str) -> None:
        """Run customer-level analysis and persist via DbSaver. No-ops on
        any failure -- the visit row is already in DB without it.

        ``items_payload`` carries the full per-item planning context
        (cycle, days_since, frequency, was_sold) so the prompt's
        EXAMPLE STRUCTURE -- which already cites those fields -- has
        ground truth to draw from. Without these the LLM either
        hallucinated frequencies or fell back to generic prose.
        """
        cust = session.customers.get(customer_code)
        if cust is None:
            return
        items_payload = [
            {
                "item_code": it.item_code,
                "item_name": it.item_name,
                "recommended_qty": int(it.recommended_qty),
                "actual_qty": int(it.actual_qty),
                "tier": it.tier,
                "purchase_cycle_days": float(it.purchase_cycle_days or 0.0),
                "days_since_last_purchase": int(it.days_since_last_purchase or 0),
                "frequency_percent": float(it.frequency_percent or 0.0),
                "was_sold": bool(it.was_sold),
            }
            for it in cust.items
        ]
        analysis = self._llm.analyze_customer(
            customer_code=customer_code,
            route_code=session.route_code,
            date=session.date,
            current_items=items_payload,
            performance_score=cust.score.score,
            coverage=cust.score.coverage,
            accuracy=cust.score.accuracy,
        )
        if analysis is not None:
            self._db.save_customer_analysis(session.to_dict(), customer_code, analysis)

    def _fire_route_llm(self, session: Session) -> None:
        """Run end-of-day route summary covering BOTH planned and
        unplanned visits.

        Route totals (``total_actual`` / ``total_recommended``) are
        derived from the **same** per-customer rows the prompt's body
        renders, NOT from ``session.total_*``. This guarantees the
        header line and the per-customer table cannot disagree -- the
        contradiction "achieving 3566 units but every customer at 0%"
        that earlier route summaries produced is impossible by
        construction once the two reads share one source.
        """
        visited_customers = [c for c in session.customers.values() if c.visited]
        visited = [
            {
                "customer_code": c.customer_code,
                "customer_name": c.customer_name,
                "is_planned": not is_unplanned_customer(c),
                "score": float(c.score.score),
                "coverage": float(c.score.coverage),
                "accuracy": float(c.score.accuracy),
                "total_actual": int(c.total_actual),
                "total_recommended": int(c.total_recommended),
                "item_count": len(c.items),
                "items_sold": c.items_sold,
            }
            for c in visited_customers
        ]
        planned_count = sum(
            1 for c in session.customers.values()
            if not is_unplanned_customer(c)
        )
        # Derived from the per-customer rows above -- identical source,
        # zero risk of header/body contradiction in the prompt.
        derived_total_actual = sum(v["total_actual"] for v in visited)
        derived_total_recommended = sum(v["total_recommended"] for v in visited)
        analysis = self._llm.analyze_route(
            route_code=session.route_code,
            date=session.date,
            visited_customers=visited,
            total_customers=planned_count,
            total_actual=derived_total_actual,
            total_recommended=derived_total_recommended,
            actual_customer_codes=[c.customer_code for c in visited_customers],
        )
        if analysis is not None:
            self._db.save_route_analysis(session.to_dict(), analysis)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------


    def _configured_routes(self) -> List[str]:
        """Routes to monitor each tick. Falls back to demand-forecasting's
        ``live_route_codes`` (already env-configured) when the supervision
        side hasn't been pinned to its own list -- one source of truth."""
        explicit = list(self._s.auto_visit_route_codes or [])
        if explicit:
            return [str(r).strip() for r in explicit if str(r).strip()]
        # Reuse the demand-forecasting fleet so ops don't double-configure.
        try:
            from demand_forecasting_pipeline.config.settings import get_settings as df_settings
            return [str(r).strip() for r in (df_settings().live_route_codes or []) if str(r).strip()]
        except Exception:
            return []
