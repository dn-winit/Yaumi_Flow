import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
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
  RedistributionView,
  SavedVisit,
  SessionSummary,
  SessionVisitTotals,
  VisitResultPayload,
} from "@/types/supervision";
import CustomerVisit from "./CustomerVisit";
import CustomerAnalysisModal, { type CustomerAnalysisContext } from "./CustomerAnalysisModal";
import RouteAnalysisModal, { type RouteAnalysisContext } from "./RouteAnalysisModal";
import UnplannedVisits from "./UnplannedVisits";
import { pluralise } from "./RedistributionSection";

import { GOOD_SCORE_THRESHOLD, fmtNum } from "@/lib/format";
import { fmtDate } from "@/lib/date";

// Cap on parallel auto-visit POSTs; prevents pool starvation on mount-time bursts.
const AUTO_VISIT_MAX_INFLIGHT = 8;

interface LiveSessionTabProps {
  sessionId: string;
  sessionData: SessionSummary | null;
  routeCode: string;
  date: string;
  /** ContextStrip actions injected by the parent (e.g. drawer triggers). */
  extraActions?: ReactNode;
  /** Pure navigation; each visit auto-persists so no save step is needed. */
  onPickAnotherRoute?: () => void;
}

interface CustomerItem {
  itemCode: string;
  itemName?: string;
  recommendedQty: number;
  // Original PascalCase rec from recommended_order; single source of truth for
  // the explainability modal + analytics payloads (no camelCase shadow).
  rec: Record<string, unknown>;
}

interface CustomerData {
  customerCode: string;
  customerName: string;
  items: CustomerItem[];
  /** Server-pre-computed; pulled from customer_tiles[*].total_units. */
  totalUnits: number;
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
  // Per-item actuals so drill-in/out re-renders without re-querying.
  actualSales: Record<string, number>;
  // Server-owned grouping (process_visit); client renders verbatim.
  redistributions: RedistributionView;
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
  const qc = useQueryClient();
  const [visits, setVisits] = useState<Record<string, VisitRecord>>({});
  // Tile-grid drill-in: null = show grid, set = show that customer's visit.
  const [selectedCustomerCode, setSelectedCustomerCode] = useState<string | null>(null);

  // AI modals
  const [custCtx, setCustCtx] = useState<CustomerAnalysisContext | null>(null);
  const [routeModalOpen, setRouteModalOpen] = useState(false);

  // Hydrate already-visited customers so the green dot + score render immediately
  // and CustomerVisit skips the briefing dance via initialVisit.
  const { data: savedVisitsData } = useSavedVisits(routeCode, date);
  const savedVisits: Record<string, SavedVisit> = useMemo(
    () => savedVisitsData?.visits ?? {},
    [savedVisitsData?.visits],
  );

  useEffect(() => {
    const entries = Object.entries(savedVisits);
    if (entries.length === 0) return;
    setVisits((prev) => {
      // Live in-session writes win over the saved snapshot (load-bearing guard;
      // prevents flicker when this query reruns after a fresh /visit).
      const next = { ...prev };
      for (const [code, sv] of entries) {
        if (next[code] != null) continue;
        next[code] = {
          customerCode: code,
          score: sv.score,
          actualQty: sv.totalActual,
          recommendedQty: sv.totalRecommended,
          actualSales: sv.actualSales,
          redistributions: sv.redistributions,
          // Off-plan rows (original_recommended_qty=0) survive refresh.
          alsoBought: sv.alsoBought ?? [],
        };
      }
      return next;
    });
  }, [savedVisits]);

  // Ref so an in-flight code doesn't trigger a re-render mid-request.
  const autoVisitInflight = useRef<Set<string>>(new Set());

  // Server-grouped customers + server-pre-computed total_units joined by code;
  // snake_case -> camelCase rename only, no aggregation/filtering.
  const customers = useMemo<CustomerData[]>(() => {
    if (!sessionData) return [];
    const grouped = sessionData.customers_grouped ?? [];
    const tilesArr = sessionData.customer_tiles ?? [];
    const unitsByCode = new Map<string, number>();
    for (const t of tilesArr) unitsByCode.set(t.customer_code, t.total_units);
    return grouped.map((g) => ({
      customerCode: g.customer_code,
      customerName: g.customer_name,
      totalUnits: unitsByCode.get(g.customer_code) ?? 0,
      items: g.items.map((rec) => ({
        itemCode: String(rec.ItemCode ?? ""),
        itemName: rec.ItemName as string | undefined,
        // Prefer EffectiveRecommended (supervisor-adjusted) over engine original.
        recommendedQty: Number(rec.EffectiveRecommended ?? rec.RecommendedQuantity ?? 0),
        rec,
      })),
    }));
  }, [sessionData]);

  // Static (non-visit) totals come straight from the session payload.
  const recommendationTotals = sessionData?.recommendation_totals ?? {
    items_count: 0,
    total_units: 0,
    customers_count: 0,
  };

  // Seeded from /session/initialize; saved-visits polls keep it fresh.
  // Monotonic accept prevents a slow poll from rewinding behind a fresh /visit.
  const [visitTotals, setVisitTotals] = useState<SessionVisitTotals>(() =>
    sessionData?.visit_totals ?? {
      visited_count: 0,
      total_actual: 0,
      total_recommended: 0,
      avg_score: null,
      unplanned_visited_count: 0,
    },
  );
  useEffect(() => {
    const incoming = savedVisitsData?.visit_totals;
    if (!incoming) return;
    setVisitTotals((prev) =>
      incoming.visited_count >= prev.visited_count ? incoming : prev,
    );
  }, [savedVisitsData?.visit_totals]);

  // Live-visited codes from /session/unplanned (LIVE tier).
  const { data: unplannedData } = useUnplannedVisits(sessionId);
  const liveVisitedSet = useMemo(
    () => new Set(unplannedData?.planned_visited_codes ?? []),
    [unplannedData?.planned_visited_codes],
  );

  // Both /saved AND /unplanned must land before any count renders -- avoids the
  // flicker where /saved arrives first and the badge ticks up as /unplanned folds in.
  const countsReady = savedVisitsData != null && unplannedData != null;

  // Visited count = UNION(DB-persisted visits, live YaumiLive invoices today).
  // Shows the full count from first paint instead of ticking up as the cron syncs.
  const effectiveVisitedCount = useMemo(() => {
    const plannedSet = new Set(customers.map((c) => c.customerCode));
    const savedCodes = Object.keys(savedVisits);
    const liveCodes = unplannedData?.planned_visited_codes ?? [];
    const union = new Set<string>();
    for (const c of savedCodes) {
      if (plannedSet.has(c)) union.add(c);
    }
    for (const c of liveCodes) {
      if (plannedSet.has(c)) union.add(c);
    }
    // Floor at visitTotals.visited_count so a fresh /visit can't be regressed by a stale poll.
    return Math.max(union.size, visitTotals.visited_count);
  }, [customers, savedVisits, unplannedData?.planned_visited_codes, visitTotals.visited_count]);

  // Average over the local visits dict (not visitTotals.avg_score) so the tile
  // doesn't tick per /visit response. Returns null when no scored customers exist.
  const effectiveAvgScore = useMemo<number | null>(() => {
    const entries = Object.values(visits);
    if (entries.length === 0) {
      return visitTotals.avg_score ?? null;
    }
    const scores = entries.map((v) => Number(v.score?.score ?? 0));
    const mean = scores.reduce((s, x) => s + x, 0) / scores.length;
    return Math.round(mean * 100) / 100;
  }, [visits, visitTotals.avg_score]);

  const allVisited =
    countsReady &&
    recommendationTotals.customers_count > 0 &&
    effectiveVisitedCount === recommendationTotals.customers_count;

  const handleVisitComplete = (customer: CustomerData, visit: VisitResultPayload) => {
    // All numerics server-computed; sessionTotals already folds in this visit.
    setVisits((prev) => ({
      ...prev,
      [customer.customerCode]: {
        customerCode: customer.customerCode,
        score: visit.score,
        actualQty: visit.actualQty,
        recommendedQty: visit.recommendedQty,
        actualSales: visit.actualSales,
        redistributions: visit.redistributions,
        alsoBought: visit.alsoBought,
      },
    }));
    setVisitTotals(visit.sessionTotals);
    qc.invalidateQueries({ queryKey: ["supervision-saved-visits", routeCode, date] });
  };

  const routeAnalysisCtx: RouteAnalysisContext | null = useMemo(() => {
    if (!routeCode || !date) return null;
    // Counts come from server-pre-computed totals; this is a wire adapter, not aggregation.
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

  // Initial backlog snapshot (captured once after both hydrations land).
  // The cron handles the backlog; FE only auto-fires for NEW invoices
  // that arrive while the page is open -- otherwise the tile would tick 0..N on mount.
  const initialLiveBacklogRef = useRef<Set<string> | null>(null);

  // Auto-fire /visit for invoices that land AFTER page-open; capped at AUTO_VISIT_MAX_INFLIGHT.
  useEffect(() => {
    if (!sessionId || customers.length === 0) return;
    // BOTH /saved and /unplanned must land before snapshotting the backlog --
    // otherwise /unplanned arriving later would treat every code as "new".
    if (savedVisitsData == null) return;
    if (unplannedData == null) return;
    // Snapshot on first observation after hydration; stable for the component lifetime.
    if (initialLiveBacklogRef.current === null) {
      initialLiveBacklogRef.current = new Set(liveVisitedSet);
      return;
    }
    const customerByCode = new Map(customers.map((c) => [c.customerCode, c]));
    for (const code of liveVisitedSet) {
      if (autoVisitInflight.current.size >= AUTO_VISIT_MAX_INFLIGHT) break;
      // Skip backlog (cron's job). Skip already-visited; check savedVisits too
      // because the hydration setVisits hasn't committed in the same render.
      if (initialLiveBacklogRef.current.has(code)) continue;
      if (visits[code] || savedVisits[code]) continue;
      if (autoVisitInflight.current.has(code)) continue;
      const cust = customerByCode.get(code);
      if (!cust) continue;
      autoVisitInflight.current.add(code);
      supervisionApi
        .processVisit(sessionId, code)
        .then((res) => {
          if (res?.success && res.visit) {
            handleVisitComplete(cust, res.visit);
          }
        })
        .catch(() => {/* logged server-side; next poll retries */})
        .finally(() => {
          autoVisitInflight.current.delete(code);
        });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveVisitedSet, visits, savedVisits, sessionId, customers, savedVisitsData, unplannedData]);

  // Lazy-fetch on customer open for live-but-not-persisted backlog codes; one /visit
  // so the modal renders real data. Same inflight gate as the auto loop -> no double-process.
  useEffect(() => {
    if (!selectedCustomerCode) return;
    if (!sessionId) return;
    if (visits[selectedCustomerCode]) return;
    if (savedVisits[selectedCustomerCode]) return;
    if (!liveVisitedSet.has(selectedCustomerCode)) return;
    if (autoVisitInflight.current.has(selectedCustomerCode)) return;
    const cust = customers.find((c) => c.customerCode === selectedCustomerCode);
    if (!cust) return;
    autoVisitInflight.current.add(selectedCustomerCode);
    supervisionApi
      .processVisit(sessionId, selectedCustomerCode)
      .then((res) => {
        if (res?.success && res.visit) {
          handleVisitComplete(cust, res.visit);
        }
      })
      .catch(() => {/* logged server-side; modal renders placeholder */})
      .finally(() => {
        autoVisitInflight.current.delete(selectedCustomerCode);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedCustomerCode, sessionId, liveVisitedSet, visits, savedVisits, customers]);

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
                {countsReady
                  ? `${effectiveVisitedCount} / ${recommendationTotals.customers_count}`
                  : `... / ${recommendationTotals.customers_count}`}
              </Badge>
            ),
          },
        ]}
        actions={
          <>
            {extraActions}
            {onPickAnotherRoute && (
              <Button variant="primary" size="sm" onClick={onPickAnotherRoute}>
                Pick another route
              </Button>
            )}
          </>
        }
      />

      {/* Metric row -- every value server-pre-computed. */}
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
          loading={!countsReady}
          value={`${effectiveVisitedCount} / ${recommendationTotals.customers_count}`}
          subtitle={(() => {
            const base = allVisited ? "All done" : "In progress";
            const dropIns = unplannedData?.unplanned_count ?? 0;
            if (dropIns <= 0) return base;
            return `${base} - ${pluralise(dropIns, "walk-in")}`;
          })()}
          trend={allVisited ? "up" : undefined}
          disableAnimation
        />
        <MetricCard
          label="Avg visit score"
          loading={!countsReady}
          value={
            effectiveAvgScore != null
              ? `${effectiveAvgScore.toFixed(1)}%`
              : "--"
          }
          subtitle={
            effectiveAvgScore != null
              ? "Planned customers only"
              : "No planned visits yet"
          }
          trend={
            effectiveAvgScore != null && effectiveAvgScore >= GOOD_SCORE_THRESHOLD
              ? "up"
              : effectiveAvgScore != null
              ? "down"
              : undefined
          }
          disableAnimation
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
                  <svg
                    className="mr-1 inline-block h-4 w-4"
                    fill="none"
                    stroke="currentColor"
                    viewBox="0 0 24 24"
                    aria-hidden="true"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M15 19l-7-7 7-7"
                    />
                  </svg>
                  Back to customers
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
                  // Briefing hydrates for every planned customer; modal skips a fresh LLM call.
                  initialBriefing={
                    savedVisitsData?.briefings?.[selected.customerCode] ?? null
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

          // Green-dot = "bought something today": actualQty>0 in session OR totalActual>0 in saved.
          // Zero-actual customers stay un-dotted to avoid "green tick + zero everywhere".
          const serverTiles = sessionData?.customer_tiles ?? [];
          const tiles: CustomerStat[] = serverTiles.map((t) => {
            const live = visits[t.customer_code];
            const saved = savedVisits[t.customer_code];
            const bought =
              (live != null && live.actualQty > 0) ||
              (saved != null && saved.totalActual > 0);
            return {
              customerCode: t.customer_code,
              customerName: t.customer_name,
              uniqueSkus: t.unique_skus,
              totalUnits: t.total_units,
              visited: bought,
              liveVisited: liveVisitedSet.has(t.customer_code),
            };
          });

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
      {countsReady && effectiveVisitedCount > 0 && (
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
              : `${effectiveVisitedCount} of ${recommendationTotals.customers_count} visited. Each visit auto-saves; pull an interim route review whenever you like.`}
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

/** Two-tab shell: Planned (static) + Unplanned (polled live; shared query key). */
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
          { key: "unplanned", label: `Walk-in (${unplannedCount})` },
        ]}
        activeTab={active}
        onTabChange={(k) => setActive(k as "planned" | "unplanned")}
      />
      {active === "planned" ? renderPlanned() : <UnplannedVisits sessionId={sessionId} />}
    </div>
  );
}
