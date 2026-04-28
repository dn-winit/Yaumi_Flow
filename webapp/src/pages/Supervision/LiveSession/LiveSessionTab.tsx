import { useMemo, useState, type ReactNode } from "react";
import Badge from "@/components/ui/Badge";
import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";
import ContextStrip from "@/components/ui/ContextStrip";
import KpiRow from "@/components/ui/KpiRow";
import Tabs from "@/components/ui/Tabs";
import CustomerGrid, { type CustomerStat } from "@/components/ui/CustomerGrid";
import MetricCard from "@/components/charts/MetricCard";
import { supervisionApi } from "@/api/supervision";
import { useUnplannedVisits } from "@/hooks/useSupervision";
import CustomerVisit from "./CustomerVisit";
import CustomerAnalysisModal, { type CustomerAnalysisContext } from "./CustomerAnalysisModal";
import RouteAnalysisModal, { type RouteAnalysisContext } from "./RouteAnalysisModal";
import UnplannedVisits from "./UnplannedVisits";

import { GOOD_SCORE_THRESHOLD, fmtNum } from "@/lib/format";
import { fmtDate } from "@/lib/date";
import { useToast } from "@/hooks/useToast";

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
   * Called when the supervisor wants to pick a different route. Renders
   * a "Pick another route →" CTA after a successful save so there's an
   * obvious next step instead of a dead-end "Saved" indicator.
   */
  onPickAnotherRoute?: () => void;
}

interface CustomerItem {
  itemCode: string;
  itemName?: string;
  recommendedQty: number;
  tier?: string;
  source?: string;
  whyItem?: string;
  whyQuantity?: string;
  purchaseCycleDays?: number;
  daysSinceLastPurchase?: number;
  frequencyPercent?: number;
  trendFactor?: number;
  // Raw PascalCase recommendation row -- carried unchanged so the
  // RecommendationModal can render the full explainability (Signals,
  // WhyItem, WhyQuantity, Confidence, etc.) without a second mapping.
  raw: Record<string, unknown>;
}

interface CustomerData {
  customerCode: string;
  customerName: string;
  items: CustomerItem[];
}

interface VisitRecord {
  customerCode: string;
  score: { score: number; coverage: number; accuracy: number };
  actualQty: number;
  recommendedQty: number;
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

  // Save flow
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const { toast } = useToast();

  // AI modals
  const [custCtx, setCustCtx] = useState<CustomerAnalysisContext | null>(null);
  const [routeModalOpen, setRouteModalOpen] = useState(false);

  // Normalise recommendation rows into a per-customer structure. Drops
  // any line where recommendedQty <= 0 -- a quantity of zero means the
  // model decided not to recommend the item, so showing it would be
  // misleading. Customers left with no items after filtering are also
  // dropped (nothing to record for them).
  const customers = useMemo<CustomerData[]>(() => {
    if (!sessionData) return [];
    const recs = (sessionData.recommendations as Record<string, unknown>[]) ?? [];
    const pick = (r: Record<string, unknown>, ...keys: string[]): unknown => {
      for (const k of keys) if (r[k] != null) return r[k];
      return undefined;
    };
    const grouped = new Map<string, CustomerData>();
    for (const rec of recs) {
      const code = String(pick(rec, "CustomerCode", "customerCode", "customer_code") ?? "");
      if (!code) continue;
      const qty = Number(pick(rec, "RecommendedQuantity", "recommendedQty", "recommended_qty") ?? 0);
      if (!(qty > 0)) continue;
      const name = String(pick(rec, "CustomerName", "customerName", "customer_name") ?? code);
      if (!grouped.has(code)) grouped.set(code, { customerCode: code, customerName: name, items: [] });
      grouped.get(code)!.items.push({
        itemCode: String(pick(rec, "ItemCode", "itemCode", "item_code") ?? ""),
        itemName: pick(rec, "ItemName", "itemName", "item_name") as string | undefined,
        recommendedQty: qty,
        tier: pick(rec, "Tier", "tier") as string | undefined,
        source: pick(rec, "Source", "source") as string | undefined,
        whyItem: pick(rec, "WhyItem", "whyItem") as string | undefined,
        whyQuantity: pick(rec, "WhyQuantity", "whyQuantity") as string | undefined,
        purchaseCycleDays: pick(rec, "PurchaseCycleDays", "purchaseCycleDays") as number | undefined,
        daysSinceLastPurchase: pick(rec, "DaysSinceLastPurchase", "daysSinceLastPurchase") as number | undefined,
        frequencyPercent: pick(rec, "FrequencyPercent", "frequencyPercent") as number | undefined,
        trendFactor: pick(rec, "TrendFactor", "trendFactor") as number | undefined,
        raw: rec,
      });
    }
    return Array.from(grouped.values()).filter((c) => c.items.length > 0);
  }, [sessionData]);

  const totals = useMemo(() => {
    const uniqueItems = new Set<string>();
    let totalUnits = 0;
    customers.forEach((c) =>
      c.items.forEach((it) => {
        uniqueItems.add(it.itemCode);
        totalUnits += it.recommendedQty;
      }),
    );
    const itemsCount = uniqueItems.size;
    const custCount = customers.length;
    const visitedCount = Object.keys(visits).length;
    const visitedScores = Object.values(visits).map((v) => v.score.score);
    const avgScore = visitedScores.length > 0
      ? visitedScores.reduce((a, b) => a + b, 0) / visitedScores.length
      : null;
    const totalActual = Object.values(visits).reduce((n, v) => n + v.actualQty, 0);
    const totalRecommended = Object.values(visits).reduce((n, v) => n + v.recommendedQty, 0);
    return { itemsCount, totalUnits, custCount, visitedCount, avgScore, totalActual, totalRecommended };
  }, [customers, visits]);

  const allVisited = customers.length > 0 && totals.visitedCount === customers.length;

  const handleVisitComplete = (customer: CustomerData, visitResult: Record<string, unknown>) => {
    const score = visitResult.score as VisitRecord["score"] | undefined;
    if (!score) return;
    // Sum actuals from input DOM is impractical; pull from visitResult if present,
    // otherwise derive from customer items + actuals in CustomerVisit (best-effort).
    const unsold = (visitResult.unsoldItems ?? {}) as Record<string, number>;
    const recommendedTotal = customer.items.reduce((n, i) => n + i.recommendedQty, 0);
    const unsoldTotal = Object.values(unsold).reduce((n, v) => n + Number(v || 0), 0);
    const actualTotal = Math.max(0, recommendedTotal - unsoldTotal);

    setVisits((prev) => ({
      ...prev,
      [customer.customerCode]: {
        customerCode: customer.customerCode,
        score,
        actualQty: actualTotal,
        recommendedQty: recommendedTotal,
      },
    }));
  };

  const handleSave = async () => {
    if (!sessionId) return;
    setSaving(true);
    setSaveErr(null);
    try {
      const res = await supervisionApi.saveActiveSession(sessionId);
      if (!res?.success) {
        const msg = String(res?.error ?? "Save failed");
        setSaveErr(msg);
        toast(msg, "danger");
        return;
      }
      setSaved(true);
      // File save always succeeds when we reach here. DB save is
      // best-effort: surface any warning so the supervisor knows the
      // session is safe on disk but the DB push needs a follow-up.
      if (res.warning) {
        setSaveErr(res.warning);
        toast(res.warning, "danger");
      } else {
        toast("Session saved", "success");
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Save failed";
      setSaveErr(msg);
      toast(msg, "danger");
    } finally {
      setSaving(false);
    }
  };

  const routeAnalysisCtx: RouteAnalysisContext | null = useMemo(() => {
    if (!routeCode || !date) return null;
    const visitedArr = Object.values(visits).map((v) => ({
      customer_code: v.customerCode,
      score: v.score.score,
      coverage: v.score.coverage,
      accuracy: v.score.accuracy,
      total_actual: v.actualQty,
      total_recommended: v.recommendedQty,
    }));
    return {
      routeCode,
      date,
      visitedCustomers: visitedArr,
      totalCustomers: customers.length,
      totalActual: totals.totalActual,
      totalRecommended: totals.totalRecommended,
      actualCustomerCodes: Object.keys(visits),
    };
  }, [routeCode, date, visits, customers.length, totals]);

  // Live-visited codes drive the small "visited live" indicator on planned
  // customer cards. Reuses the same React Query key as VisitsTabs / UnplannedVisits
  // so only one network request runs per polling cycle.
  const { data: unplannedData } = useUnplannedVisits(sessionId);
  const liveVisitedSet = useMemo(
    () => new Set(unplannedData?.planned_visited_codes ?? []),
    [unplannedData?.planned_visited_codes],
  );

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
                {totals.visitedCount} / {totals.custCount}
              </Badge>
            ),
          },
        ]}
        actions={
          <>
            {extraActions}
            {saved && <span className="text-caption text-success-700 font-medium">Saved</span>}
            {saveErr && <span className="text-caption text-danger-600">{saveErr}</span>}
            {saved && onPickAnotherRoute ? (
              <Button variant="primary" size="sm" onClick={onPickAnotherRoute}>
                Pick another route →
              </Button>
            ) : (
              <Button
                variant="primary"
                size="sm"
                loading={saving}
                disabled={saved || totals.visitedCount === 0}
                onClick={handleSave}
              >
                Save session
              </Button>
            )}
          </>
        }
      />

      {/* Metric row */}
      <KpiRow>
        <MetricCard
          label="Different items"
          value={fmtNum(totals.itemsCount)}
          subtitle={`${fmtNum(totals.totalUnits)} units across all customers`}
        />
        <MetricCard label="Customers planned" value={fmtNum(totals.custCount)} subtitle="On today's route" />
        <MetricCard
          label="Visited"
          value={`${totals.visitedCount} / ${totals.custCount}`}
          subtitle={allVisited ? "All done" : "In progress"}
          trend={allVisited ? "up" : undefined}
        />
        <MetricCard
          label="Avg visit score"
          value={totals.avgScore != null ? `${totals.avgScore.toFixed(1)}%` : "--"}
          subtitle={totals.avgScore != null ? "Across visited customers" : "No visits yet"}
          trend={totals.avgScore != null && totals.avgScore >= GOOD_SCORE_THRESHOLD ? "up" : totals.avgScore != null ? "down" : undefined}
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
                  liveVisited={liveVisitedSet.has(selected.customerCode)}
                  onVisitComplete={(result) => handleVisitComplete(selected, result)}
                  onRequestAnalysis={(payload) =>
                    setCustCtx({
                      customerCode: payload.customerCode,
                      customerName: payload.customerName,
                      routeCode,
                      date,
                      items: payload.items,
                      score: payload.score,
                    })
                  }
                />
              </div>
            );
          }

          const tiles: CustomerStat[] = customers.map((c) => {
            const skus = new Set(c.items.map((it) => it.itemCode)).size;
            const units = c.items.reduce((n, it) => n + (it.recommendedQty || 0), 0);
            return {
              customerCode: c.customerCode,
              customerName: c.customerName,
              uniqueSkus: skus,
              totalUnits: units,
              visited: visits[c.customerCode] != null,
              liveVisited: liveVisitedSet.has(c.customerCode),
            };
          });

          return (
            <CustomerGrid
              customers={tiles}
              onSelect={setSelectedCustomerCode}
              summary={
                <>
                  Pick a customer to record the visit — {customers.length} planned for{" "}
                  <strong>{fmtDate(date)}</strong>
                </>
              }
            />
          );
        }}
      />

      {/* Route review trigger -- available once at least one visit exists */}
      {totals.visitedCount > 0 && (
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
              ? `All ${totals.custCount} customers visited. Review the AI summary, then save the session if you haven't already.`
              : `${totals.visitedCount} of ${totals.custCount} visited. You can still pull an interim route review or save progress.`}
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
