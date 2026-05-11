import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { supervisionApi } from "@/api/supervision";
import Badge from "@/components/ui/Badge";
import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";
import ContextStrip from "@/components/ui/ContextStrip";
import KpiRow from "@/components/ui/KpiRow";
import Tabs from "@/components/ui/Tabs";
import CustomerGrid, { type CustomerStat } from "@/components/ui/CustomerGrid";
import MetricCard from "@/components/charts/MetricCard";
import { useUnplannedVisits, useSavedVisits } from "@/hooks/useSupervision";
import type {
  RecommendationTotals,
  SavedVisit,
  SessionCustomerGrouped,
  SessionCustomerTile,
  VisitTotals,
} from "@/types/supervision";
import CustomerVisit from "./CustomerVisit";
import CustomerAnalysisModal, { type CustomerAnalysisContext } from "./CustomerAnalysisModal";
import RouteAnalysisModal, { type RouteAnalysisContext } from "./RouteAnalysisModal";
import UnplannedVisits from "./UnplannedVisits";

import { GOOD_SCORE_THRESHOLD, fmtNum } from "@/lib/format";
import { fmtDate } from "@/lib/date";

// Cap on parallel auto-visit POSTs. Each call queues a backend background
// task that writes three tables; spraying 30+ at once on mount would
// queue against the supervision DB pool and slow the first user
// interaction. Drained naturally as visits settle and the effect re-fires.
const AUTO_VISIT_MAX_INFLIGHT = 8;

interface LiveSessionTabProps {
  sessionId: string;
  sessionData: Record<string, unknown> | null;
  routeCode: string;
  date: string;
  /**
   * Optional workflow-level actions injected into the ContextStrip --
   * e.g. "Last 30 days" / "Upcoming week" drawer triggers from the
   * Visit step. Kept generic so this component stays unaware of the
   * specific drawers its parent owns.
   */
  extraActions?: ReactNode;
  /**
   * Called when the supervisor wants to pick a different route. Each
   * visit auto-persists to the supervision tables, so leaving the
   * session is purely a navigation action -- no save step in between.
   */
  onPickAnotherRoute?: () => void;
}

interface CustomerItem {
  itemCode: string;
  itemName?: string;
  recommendedQty: number;
  // The original PascalCase rec from recommended_order, carried verbatim
  // through the supervision session payload. Single source of truth for
  // the explainability modal AND for the analytics-API payloads -- the
  // frontend never derives a parallel camelCase shadow.
  rec: Record<string, unknown>;
}

interface CustomerData {
  customerCode: string;
  customerName: string;
  items: CustomerItem[];
  /** Server-pre-computed unit total for the customer's plan. Lifted
   *  from ``customer_tiles[*].total_units`` so downstream renderers
   *  never sum ``items[*].recommendedQty`` themselves. */
  totalUnits: number;
}

interface RedistributionRecord {
  from: string;
  to: string;
  itemCode: string;
  quantity: number;
}

interface AlsoBoughtRecord {
  item_code: string;
  qty: number;
}

interface VisitRecord {
  customerCode: string;
  score: { score: number; coverage: number; accuracy: number };
  actualQty: number;
  recommendedQty: number;
  // Per-item actuals so a drill-in/drill-out cycle re-renders the
  // visited view from the same data the freshly-completed visit
  // produced -- without re-querying the warehouse.
  actualSales: Record<string, number>;
  // Redistribution + off-plan purchases produced by ``process_visit``
  // -- carried alongside the score so the customer drill-in view
  // renders the full visit context from one source.
  redistributions: RedistributionRecord[];
  alsoBought: AlsoBoughtRecord[];
}

export default function LiveSessionTab({
  sessionId,
  sessionData,
  routeCode,
  date,
  extraActions,
  onPickAnotherRoute,
}: LiveSessionTabProps) {
  const [visits, setVisits] = useState<Record<string, VisitRecord>>({});
  // Tile-grid → drill-in: null = show grid, set = show that customer's visit.
  const [selectedCustomerCode, setSelectedCustomerCode] = useState<string | null>(null);

  // AI modals
  const [custCtx, setCustCtx] = useState<CustomerAnalysisContext | null>(null);
  const [routeModalOpen, setRouteModalOpen] = useState(false);

  // Hydrate already-visited customers from the supervision tables on
  // mount: each prior-day or earlier-today visit lands as a row in
  // visits[code] so the green dot + score render immediately, and the
  // per-customer initialVisit prop lets CustomerVisit skip the
  // briefing -> mark-visited dance for them.
  const { data: savedVisitsData } = useSavedVisits(routeCode, date);
  const savedVisits: Record<string, SavedVisit> = useMemo(
    () => savedVisitsData?.visits ?? {},
    [savedVisitsData?.visits],
  );

  useEffect(() => {
    const entries = Object.entries(savedVisits);
    if (entries.length === 0) return;
    setVisits((prev) => {
      // Live in-session edits beat the saved snapshot -- otherwise a
      // fresh visit would briefly flicker back to the stored value
      // when this query reruns.
      const next = { ...prev };
      for (const [code, sv] of entries) {
        if (next[code] != null) continue;
        next[code] = {
          customerCode: code,
          score: sv.score,
          actualQty: sv.totalActual,
          recommendedQty: sv.totalRecommended,
          actualSales: sv.actualSales,
          // Redistribution + off-plan purchases are visit-time signals
          // we don't persist (they're transient). Saved hydration
          // surfaces empty arrays; a fresh auto-visit re-populates
          // them while the supervisor is still in the session.
          redistributions: [],
          alsoBought: [],
        };
      }
      return next;
    });
  }, [savedVisits]);

  // Auto-fire bookkeeping. The actual effect runs further down where
  // ``liveVisitedSet``, ``customers`` and ``handleVisitComplete`` are
  // already in scope. Tracking is held in a ref so an in-flight code
  // doesn't trigger a re-render while the request is open.
  const autoVisitInflight = useRef<Set<string>>(new Set());

  // Customers come pre-grouped from the server (qty>0 items only,
  // empty customers dropped). Wire-level snake_case is mapped to the
  // existing camelCase shape this file already passes downstream --
  // pure presentation rename, no aggregation, no filtering.
  // ``customer_tiles`` carries the per-customer ``total_units`` count
  // pre-computed server-side; we join by customer_code so downstream
  // renderers never sum recommendedQty themselves.
  const customers = useMemo<CustomerData[]>(() => {
    if (!sessionData) return [];
    const grouped = (sessionData.customers_grouped as SessionCustomerGrouped[] | undefined) ?? [];
    const tilesArr =
      (sessionData.customer_tiles as SessionCustomerTile[] | undefined) ?? [];
    const unitsByCode = new Map<string, number>();
    for (const t of tilesArr) unitsByCode.set(t.customer_code, t.total_units);
    return grouped.map((g) => ({
      customerCode: g.customer_code,
      customerName: g.customer_name,
      totalUnits: unitsByCode.get(g.customer_code) ?? 0,
      items: g.items.map((rec) => ({
        itemCode: String(rec.ItemCode ?? ""),
        itemName: rec.ItemName as string | undefined,
        // ``EffectiveRecommended`` reflects supervisor adjustments;
        // ``RecommendedQuantity`` is the engine's original. The table
        // shows what the rep should load today, so prefer effective.
        recommendedQty: Number(rec.EffectiveRecommended ?? rec.RecommendedQuantity ?? 0),
        rec,
      })),
    }));
  }, [sessionData]);

  // Static (non-visit) totals come straight from the session payload.
  const recommendationTotals = (sessionData?.recommendation_totals as
    | RecommendationTotals
    | undefined) ?? { items_count: 0, total_units: 0, customers_count: 0 };

  // Live visit totals: hydrated from saved visits on mount, then
  // overwritten by each ``/visit`` response. The frontend never sums
  // the local visits map -- the server pushes the cumulative number
  // alongside every visit result.
  const [visitTotals, setVisitTotals] = useState<VisitTotals>({
    visited_count: 0,
    total_actual: 0,
    total_recommended: 0,
    avg_score: null,
  });
  useEffect(() => {
    if (savedVisitsData?.visit_totals) {
      setVisitTotals(savedVisitsData.visit_totals);
    }
  }, [savedVisitsData?.visit_totals]);

  const allVisited =
    recommendationTotals.customers_count > 0 &&
    visitTotals.visited_count === recommendationTotals.customers_count;

  const handleVisitComplete = (customer: CustomerData, visitResult: Record<string, unknown>) => {
    const score = visitResult.score as VisitRecord["score"] | undefined;
    if (!score) return;
    // ``actualQty`` and ``recommendedQty`` are server-computed (sum of
    // ``min(rec, act)`` and ``sum(rec)`` across the customer's planned
    // items). The frontend never derives them from unsold or summed
    // input fields. ``sessionTotals`` is the cumulative aggregate
    // including this latest visit, so the tile row updates without
    // re-summing the local visits map.
    const actualTotal = Number(visitResult.actualQty ?? 0);
    const recommendedTotal = Number(visitResult.recommendedQty ?? 0);
    const actualSales = (visitResult.actualSales ?? {}) as Record<string, number>;
    const redistributions = (visitResult.redistributions ?? []) as RedistributionRecord[];
    const alsoBought = (visitResult.alsoBought ?? []) as AlsoBoughtRecord[];
    const sessionTotals = visitResult.sessionTotals as VisitTotals | undefined;

    setVisits((prev) => ({
      ...prev,
      [customer.customerCode]: {
        customerCode: customer.customerCode,
        score,
        actualQty: actualTotal,
        recommendedQty: recommendedTotal,
        actualSales,
        redistributions,
        alsoBought,
      },
    }));
    if (sessionTotals) {
      setVisitTotals(sessionTotals);
    }
  };

  const routeAnalysisCtx: RouteAnalysisContext | null = useMemo(() => {
    if (!routeCode || !date) return null;
    // ``totalCustomers`` reads from the server-pre-computed
    // recommendation totals (the planned-customer count after the
    // qty>0 filter). ``totalActual`` / ``totalRecommended`` come from
    // the server-pushed visit totals. The visited-customer rows
    // serialise the live visits map for the LLM payload -- a wire
    // adapter, not aggregation.
    const visitedArr = Object.values(visits).map((v) => ({
      customer_code: v.customerCode,
      score: v.score.score,
      coverage: v.score.coverage,
      accuracy: v.score.accuracy,
      total_actual: v.actualQty,
      total_recommended: v.recommendedQty,
    }));
    return {
      sessionId,
      routeCode,
      date,
      visitedCustomers: visitedArr,
      totalCustomers: recommendationTotals.customers_count,
      totalActual: visitTotals.total_actual,
      totalRecommended: visitTotals.total_recommended,
      actualCustomerCodes: Object.keys(visits),
      initialAnalysis: savedVisitsData?.routeAnalysis ?? null,
    };
  }, [
    sessionId,
    routeCode,
    date,
    visits,
    recommendationTotals.customers_count,
    visitTotals.total_actual,
    visitTotals.total_recommended,
    savedVisitsData?.routeAnalysis,
  ]);

  // Live-visited codes drive the small "visited live" indicator on planned
  // customer cards. Reuses the same React Query key as VisitsTabs / UnplannedVisits
  // so only one network request runs per polling cycle.
  const { data: unplannedData } = useUnplannedVisits(sessionId);
  const liveVisitedSet = useMemo(
    () => new Set(unplannedData?.planned_visited_codes ?? []),
    [unplannedData?.planned_visited_codes],
  );

  // Auto-fire ``process_visit`` for new YaumiLive invoices that arrived
  // AFTER the page was opened. The session-init endpoint synchronously
  // reconciles the route with YaumiLive before responding, so on first
  // render the saved snapshot already reflects every customer invoiced
  // so far -- this loop only catches incremental arrivals. Without the
  // ``savedVisitsData`` gate it would race the hydration query and tick
  // the counter up 1 -> 2 -> 3 in front of the supervisor for a route
  // that was already mid-shift.
  //
  // Concurrency-bounded: we cap in-flight calls at AUTO_VISIT_MAX_INFLIGHT
  // so a burst of new invoices can't fan out N parallel DB writes.
  useEffect(() => {
    if (!sessionId || customers.length === 0) return;
    if (savedVisitsData == null) return; // wait for hydration before firing
    const customerByCode = new Map(customers.map((c) => [c.customerCode, c]));
    for (const code of liveVisitedSet) {
      if (autoVisitInflight.current.size >= AUTO_VISIT_MAX_INFLIGHT) break;
      if (visits[code]) continue;
      if (autoVisitInflight.current.has(code)) continue;
      const cust = customerByCode.get(code);
      if (!cust) continue;
      autoVisitInflight.current.add(code);
      supervisionApi
        .processVisit(sessionId, code)
        .then((res) => {
          const v = (res?.visit ?? {}) as Record<string, unknown>;
          if (v && !v.error) {
            handleVisitComplete(cust, v);
          }
        })
        .catch(() => {/* logged server-side; next poll retries */})
        .finally(() => {
          autoVisitInflight.current.delete(code);
        });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveVisitedSet, visits, sessionId, customers, savedVisitsData]);

  return (
    <div className="space-y-6">
      {/* Context strip */}
      <ContextStrip
        items={[
          { label: "Route", value: <Badge variant="info">{routeCode}</Badge> },
          { label: "Date", value: <Badge variant="neutral">{fmtDate(date)}</Badge> },
          {
            label: "Progress",
            value: (
              <Badge variant={allVisited ? "success" : "warning"}>
                {visitTotals.visited_count} / {recommendationTotals.customers_count}
              </Badge>
            ),
          },
        ]}
        actions={
          <>
            {extraActions}
            {onPickAnotherRoute && (
              <Button variant="primary" size="sm" onClick={onPickAnotherRoute}>
                Pick another route →
              </Button>
            )}
          </>
        }
      />

      {/* Metric row -- every value is server-pre-computed. Static
          recommendation counts come from session.summary().recommendation_totals;
          live visit aggregates ride on session.summary().visit_totals
          and update via /visit responses. */}
      <KpiRow>
        <MetricCard
          label="Different items"
          value={fmtNum(recommendationTotals.items_count)}
          subtitle={`${fmtNum(recommendationTotals.total_units)} units across all customers`}
        />
        <MetricCard
          label="Customers planned"
          value={fmtNum(recommendationTotals.customers_count)}
          subtitle="On today's route"
        />
        <MetricCard
          label="Visited"
          value={`${visitTotals.visited_count} / ${recommendationTotals.customers_count}`}
          subtitle={allVisited ? "All done" : "In progress"}
          trend={allVisited ? "up" : undefined}
        />
        <MetricCard
          label="Avg visit score"
          value={
            visitTotals.avg_score != null
              ? `${visitTotals.avg_score.toFixed(1)}%`
              : "--"
          }
          subtitle={
            visitTotals.avg_score != null
              ? "Across visited customers"
              : "No visits yet"
          }
          trend={
            visitTotals.avg_score != null && visitTotals.avg_score >= GOOD_SCORE_THRESHOLD
              ? "up"
              : visitTotals.avg_score != null
              ? "down"
              : undefined
          }
        />
      </KpiRow>

      {/* Planned vs unplanned visits -- two tabs */}
      <VisitsTabs
        sessionId={sessionId}
        plannedCount={customers.length}
        renderPlanned={() => {
          const selected = selectedCustomerCode
            ? customers.find((c) => c.customerCode === selectedCustomerCode)
            : null;

          if (selected) {
            return (
              <div className="space-y-3">
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setSelectedCustomerCode(null)}
                >
                  ← Back to customers
                </Button>
                <CustomerVisit
                  sessionId={sessionId}
                  routeCode={routeCode}
                  date={date}
                  customerCode={selected.customerCode}
                  customerName={selected.customerName}
                  items={selected.items}
                  totalUnits={selected.totalUnits}
                  liveVisited={liveVisitedSet.has(selected.customerCode)}
                  initialVisit={
                    visits[selected.customerCode]
                      ? {
                          score: visits[selected.customerCode].score,
                          actualSales: visits[selected.customerCode].actualSales,
                          totalActual: visits[selected.customerCode].actualQty,
                          totalRecommended: visits[selected.customerCode].recommendedQty,
                          redistributions: visits[selected.customerCode].redistributions,
                          alsoBought: visits[selected.customerCode].alsoBought,
                          preVisitBriefing:
                            savedVisits[selected.customerCode]?.preVisitBriefing ?? null,
                          customerAnalysis:
                            savedVisits[selected.customerCode]?.customerAnalysis ?? null,
                        }
                      : undefined
                  }
                  onRequestAnalysis={(payload) =>
                    setCustCtx({
                      sessionId: payload.sessionId,
                      customerCode: payload.customerCode,
                      customerName: payload.customerName,
                      routeCode,
                      date,
                      items: payload.items,
                      score: payload.score,
                      initialAnalysis: payload.initialAnalysis ?? null,
                    })
                  }
                />
              </div>
            );
          }

          // Tiles come pre-computed from the session payload --
          // ``unique_skus`` and ``total_units`` are filled server-side;
          // the only thing the client overlays is the in-session
          // ``visited`` / ``liveVisited`` flags. No counting, no sums.
          const serverTiles =
            (sessionData?.customer_tiles as SessionCustomerTile[] | undefined) ?? [];
          const tiles: CustomerStat[] = serverTiles.map((t) => ({
            customerCode: t.customer_code,
            customerName: t.customer_name,
            uniqueSkus: t.unique_skus,
            totalUnits: t.total_units,
            visited: visits[t.customer_code] != null || t.visited,
            liveVisited: liveVisitedSet.has(t.customer_code),
          }));

          return (
            <CustomerGrid
              customers={tiles}
              onSelect={setSelectedCustomerCode}
              summary={
                <>
                  Pick a customer to record the visit -{" "}
                  {recommendationTotals.customers_count} planned for{" "}
                  <strong>{fmtDate(date)}</strong>
                </>
              }
            />
          );
        }}
      />

      {/* Route review trigger -- available once at least one visit exists */}
      {visitTotals.visited_count > 0 && (
        <Card
          title={allVisited ? "Route complete" : "Route in progress"}
          actions={
            <Button variant="secondary" size="sm" onClick={() => setRouteModalOpen(true)}>
              Get route review
            </Button>
          }
        >
          <p className="text-body text-text-secondary">
            {allVisited
              ? `All ${recommendationTotals.customers_count} customers visited. Each visit is already saved -- review the AI summary or pick another route.`
              : `${visitTotals.visited_count} of ${recommendationTotals.customers_count} visited. Each visit auto-saves; pull an interim route review whenever you like.`}
          </p>
        </Card>
      )}

      {/* AI modals */}
      <CustomerAnalysisModal
        open={custCtx != null}
        onClose={() => setCustCtx(null)}
        ctx={custCtx}
      />
      <RouteAnalysisModal
        open={routeModalOpen}
        onClose={() => setRouteModalOpen(false)}
        ctx={routeAnalysisCtx}
      />
    </div>
  );
}

/**
 * Two-tab shell: "Planned" (static-per-session) + "Unplanned" (polled live).
 *
 * Unplanned count in the tab label comes from the same React Query key that
 * UnplannedVisits consumes -- single network request, single cache entry.
 */
function VisitsTabs({
  sessionId,
  plannedCount,
  renderPlanned,
}: {
  sessionId: string;
  plannedCount: number;
  renderPlanned: () => ReactNode;
}) {
  const [active, setActive] = useState<"planned" | "unplanned">("planned");
  const { data } = useUnplannedVisits(sessionId);
  const unplannedCount = data?.unplanned_count ?? 0;

  return (
    <div className="space-y-4">
      <Tabs
        tabs={[
          { key: "planned", label: `Planned (${plannedCount})` },
          { key: "unplanned", label: `Unplanned (${unplannedCount})` },
        ]}
        activeTab={active}
        onTabChange={(k) => setActive(k as "planned" | "unplanned")}
      />
      {active === "planned" ? renderPlanned() : <UnplannedVisits sessionId={sessionId} />}
    </div>
  );
}
