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
    new_planned_visits: int = 0
    new_unplanned_visits: int = 0
    skipped_existing: int = 0
    # LLM call counts -- one tick exposes how the in-flow pipeline
    # behaved without needing to query the DB.
    llm_briefing_calls: int = 0
    llm_customer_calls: int = 0
    llm_route_calls: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "route_code": self.route_code,
            "date": self.date,
            "new_planned_visits": self.new_planned_visits,
            "new_unplanned_visits": self.new_unplanned_visits,
            "skipped_existing": self.skipped_existing,
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
        if any(r.get("new_planned_visits") or r.get("new_unplanned_visits") for r in out):
            logger.info("Auto-visit tick summary: %s", out)
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
            already_persisted: Set[str] = self._db.list_visited_customer_codes(
                session.route_code, session.date,
            )
            np_, nu_, sk_ = self._sync_visits_parallel(
                session, invoiced, already_persisted,
            )
            report.new_planned_visits = np_
            report.new_unplanned_visits = nu_
            report.skipped_existing = sk_

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
        already_persisted: Set[str],
    ) -> Tuple[int, int, int]:
        """Walk YaumiLive invoices, upsert new visits in parallel.

        ``invoiced`` is the response from ``LiveActualsClient.get_route_sales``,
        which already carries each customer's invoiced items inline:
        ``[{customer_code, customer_name, items: [{item_code, qty}, ...]}, ...]``.
        We extract per-customer actuals from that single round-trip
        instead of re-querying ``get_actuals`` per customer -- the
        previous N+1 fetch pattern wasted N HTTP round-trips and N DB
        connections per tick.

        Each worker does only the in-memory state mutation + snapshot
        capture under the session lock, then runs the DB upsert outside
        the lock. Returns ``(new_planned, new_unplanned, skipped_existing)``.
        """
        cache = self._sessions[session.session_id]
        lock = cache.lock

        new_planned = 0
        new_unplanned = 0
        skipped = 0
        # Filter ahead of the pool so workers only see real work, and
        # pre-extract the actuals dict from the embedded items list so
        # workers don't repeat the parsing.
        targets: List[Tuple[str, str, Dict[str, int]]] = []
        for visitor in invoiced:
            code = str(visitor.get("customer_code") or "").strip()
            if not code:
                continue
            if code in already_persisted:
                skipped += 1
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
            return new_planned, new_unplanned, skipped

        def _process(target: Tuple[str, str, Dict[str, int]]) -> str:
            code, cust_name, actuals = target
            # In-memory mutation + snapshot capture -- inside lock.
            with lock:
                if code in session.customers:
                    kind = "planned"
                    self._mgr.process_visit(session, code, actuals)
                else:
                    kind = "unplanned"
                    self._register_unplanned(session, code, cust_name, actuals)
                snapshot = session.to_dict()
            # DB write -- outside lock (cap=auto_visit_data_phase_workers in flight).
            self._db.upsert_visit(snapshot, code)
            return kind

        with ThreadPoolExecutor(
            max_workers=self._s.auto_visit_data_phase_workers,
            thread_name_prefix=f"av-data-{session.route_code}",
        ) as pool:
            for kind in pool.map(_process, targets):
                if kind == "planned":
                    new_planned += 1
                else:
                    new_unplanned += 1

        return new_planned, new_unplanned, skipped

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
            # Skip unplanned drop-ins -- nothing to brief on (their
            # items list reflects what they bought, not a plan). A
            # briefing-only stub with no items is treated as planned
            # (matches the shared classifier).
            if not cust.items or is_unplanned_customer(cust):
                continue
            targets.append(code)
        if not targets:
            return 0

        def _fire(code: str) -> int:
            cust = session.customers.get(code)
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
                customer_code=code,
                customer_name=cust.customer_name,
                route_code=session.route_code,
                date=session.date,
                items=items_payload,
            )
            # Always run save_pre_visit_briefing -- even when the LLM
            # returned None. The shared save path upserts the route header
            # and the customer row first, then conditionally writes the
            # briefing column when content is non-empty. So an LLM failure
            # still produces a customer-table row (briefing column NULL,
            # ready for retry) and customers_planned in the route header
            # always matches the customer-table cardinality.
            self._db.save_pre_visit_briefing(snapshot, code, briefing)
            return 1 if briefing else 0

        with ThreadPoolExecutor(
            max_workers=self._s.auto_visit_llm_phase_workers,
            thread_name_prefix=f"av-llm-brief-{session.route_code}",
        ) as pool:
            return sum(pool.map(_fire, targets))

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
            # Refresh the planned customer set from current
            # recommendations -- a customer added to today's plan after
            # the original session was created should still be picked
            # up by future briefings / visits without losing the saved
            # session_id.
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
        """Add an unplanned customer to the session: items list comes from
        what they actually invoiced (recommended_qty=0). Marked visited
        so the DB upsert produces a row."""
        items = [
            SessionItem(
                item_code=str(ic), item_name="",
                recommended_qty=0, actual_qty=int(qty), was_sold=int(qty) > 0,
                tier=TIER_UNPLANNED,
                # Minimal synthetic rec so SessionItem.to_dict() emits the
                # canonical ItemCode / ItemName / Tier / RecommendedQuantity
                # fields downstream consumers (DB writer, frontend) read.
                raw={
                    "ItemCode": str(ic),
                    "ItemName": "",
                    "RecommendedQuantity": 0,
                    "Tier": TIER_UNPLANNED,
                },
            )
            for ic, qty in actual_sales.items() if int(qty) > 0
        ]
        seq = session.visit_sequence_counter + 1
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
        any failure -- the visit row is already in DB without it."""
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
        unplanned visits. Each visited customer carries an
        ``is_planned`` flag so the LLM can split the narrative
        between adoption (planned visits) and out-of-plan
        purchasing (drop-ins). ``total_customers`` is the count of
        planned customers only -- the UI's completion ratio
        (visited / planned) reads from the same denominator."""
        visited = [
            {
                "customer_code": c.customer_code,
                "customer_name": c.customer_name,
                "is_planned": not is_unplanned_customer(c),
                "score": c.score.score,
                "coverage": c.score.coverage,
                "accuracy": c.score.accuracy,
                "total_actual": c.total_actual,
                "total_recommended": c.total_recommended,
            }
            for c in session.customers.values() if c.visited
        ]
        planned_count = sum(
            1 for c in session.customers.values()
            if not is_unplanned_customer(c)
        )
        analysis = self._llm.analyze_route(
            route_code=session.route_code,
            date=session.date,
            visited_customers=visited,
            total_customers=planned_count,
            total_actual=float(session.total_actual),
            total_recommended=float(session.total_recommended),
            actual_customer_codes=[c.customer_code for c in session.customers.values() if c.visited],
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
