import { useMemo, useState } from "react";
import Card from "@/components/ui/Card";
import Button from "@/components/ui/Button";
import ContextStrip from "@/components/ui/ContextStrip";
import DatePicker from "@/components/ui/DatePicker";
import Select from "@/components/ui/Select";
import Loading from "@/components/ui/Loading";
import EmptyState from "@/components/ui/EmptyState";
import { useFilterOptions, useRecommendations, useGenerate } from "@/hooks/useRecommendedOrder";
import { useToast } from "@/hooks/useToast";
import RoutePickerGrid from "@/pages/RecommendedOrders/RoutePickerGrid";
import CustomerRecommendationsPanel from "@/pages/RecommendedOrders/CustomerRecommendationsPanel";

import InfoPanel from "@/components/ui/InfoPanel";
import { RECOMMENDED_ORDERS_INFO } from "@/config/module-info";
import AdoptionDrawer from "./AdoptionDrawer";
import UpcomingPlanDrawer from "./UpcomingPlanDrawer";
import { useWorkflow } from "../workflowContext";

export default function OrdersTab() {
  const { date, setDate } = useWorkflow();
  // Route selection is tab-local so leaving and returning to Orders always
  // starts at the route-picker grid.
  const [routeCode, setRouteCode] = useState("");

  const { data: filterOptions, loading: optionsLoading } = useFilterOptions(date);

  const params = useMemo(
    () => ({ date, route_code: routeCode || undefined, limit: 5000, offset: 0 }),
    [date, routeCode]
  );

  const {
    data: recsData,
    loading: recsLoading,
    error: recsError,
    refetch: refetchRecs,
  } = useRecommendations(params);

  const { execute: generate, loading: generating, error: generateError } = useGenerate();

  const [adoptionOpen, setAdoptionOpen] = useState(false);
  const [upcomingOpen, setUpcomingOpen] = useState(false);
  const { toast } = useToast();

  const handleGenerateRoute = async () => {
    if (!routeCode) return;
    try {
      await generate(date, [routeCode], true);
      refetchRecs();
      toast("Recommendations generated", "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "Generation failed", "danger");
    }
  };

  const routeStats = useMemo(() => {
    if (routeCode || !recsData?.data) return undefined;
    const stats: Record<string, { customers: number; skus: number; totalQty: number }> = {};
    const customerSets: Record<string, Set<string>> = {};
    const itemSets: Record<string, Set<string>> = {};
    for (const rec of recsData.data) {
      const rc = rec.RouteCode;
      if (!stats[rc]) {
        stats[rc] = { customers: 0, skus: 0, totalQty: 0 };
        customerSets[rc] = new Set();
        itemSets[rc] = new Set();
      }
      customerSets[rc].add(rec.CustomerCode);
      itemSets[rc].add(rec.ItemCode);
      stats[rc].totalQty += rec.RecommendedQuantity;
    }
    for (const code of Object.keys(stats)) {
      stats[code].customers = customerSets[code].size;
      stats[code].skus = itemSets[code].size;
    }
    return stats;
  }, [recsData, routeCode]);

  const routes = filterOptions?.routes ?? [];
  const justGenerated = recsData?.source === "generated";
  const loadingMsg = recsLoading
    ? routeCode
      ? `Loading route ${routeCode}...`
      : "Loading recommendations (generating on first access)..."
    : null;

  const renderBody = () => {
    if (optionsLoading || recsLoading) {
      return <Loading message={loadingMsg ?? "Loading..."} />;
    }

    const hasData = recsData?.data && recsData.data.length > 0;

    if (!routeCode) {
      return (
        <RoutePickerGrid
          date={date}
          routes={routes}
          routeStats={routeStats}
          journeyCounts={filterOptions?.journey_counts}
          routeDiagnoses={filterOptions?.route_diagnoses}
          onRouteSelect={setRouteCode}
          onGenerated={refetchRecs}
        />
      );
    }

    if (!hasData) {
      const diag = recsData?.diagnosis;
      // The diagnosis is computed by the backend after a generation pass; if
      // it's present we know exactly WHY the route is empty and can show a
      // positively-framed, actionable message. Falling back to the generic
      // "trigger generation" CTA only when no diagnosis is available (cold
      // start / before first generation attempt).
      const fallbackTitle = "No recommendations yet";
      const fallbackDetail = `No recommendations for route ${routeCode} on ${date}. Auto-generation runs nightly -- or trigger it manually now.`;
      return (
        <Card>
          <EmptyState
            icon={diag ? "🔔" : "📦"}
            title={diag?.headline ?? fallbackTitle}
            message={diag?.detail ?? fallbackDetail}
            action={
              <div className="flex flex-col items-stretch gap-3">
                {diag && diag.customers.length > 0 && (
                  <div className="bg-surface-sunken border border-subtle rounded-lg px-4 py-3 space-y-2 text-left">
                    {diag.customers.map((c) => (
                      <div key={c.customer_code} className="text-body text-text-secondary">
                        <span className="font-semibold text-text-primary">
                          {c.customer_name?.trim() || c.customer_code}
                        </span>
                        {c.typical_items.length > 0 && (
                          <span className="text-text-tertiary">
                            {" "}
                            usually buys{" "}
                            {c.typical_items
                              .map((it) => it.name?.trim() || it.code)
                              .join(", ")}
                          </span>
                        )}
                      </div>
                    ))}
                  </div>
                )}
                <Button
                  variant={diag ? "ghost" : "primary"}
                  size="sm"
                  loading={generating}
                  onClick={handleGenerateRoute}
                >
                  {diag ? "Re-run generation" : `Generate for route ${routeCode}`}
                </Button>
                {generateError && <p className="text-body text-danger-600">{generateError}</p>}
              </div>
            }
          />
        </Card>
      );
    }

    return (
      <CustomerRecommendationsPanel recommendations={recsData!.data} />
    );
  };

  return (
    <div className="space-y-6">
      {/* Single header strip: context + analytics + info + back. Renders as
          soon as a route is picked, regardless of whether recs exist yet. */}
      {routeCode ? (
        <ContextStrip
          items={[
            { label: "Route", value: routeCode },
            { label: "Date", value: date },
            ...(recsData?.data?.length
              ? [{ label: "Recommendations", value: recsData.total }]
              : []),
            ...(justGenerated
              ? [{ label: "Status", value: "just generated" }]
              : []),
          ]}
          actions={
            <>
              <Button variant="secondary" size="sm" onClick={() => setAdoptionOpen(true)}>
                Last 30 days
              </Button>
              <Button variant="secondary" size="sm" onClick={() => setUpcomingOpen(true)}>
                Upcoming week
              </Button>
              <InfoPanel {...RECOMMENDED_ORDERS_INFO} />
              <Button variant="ghost" size="sm" onClick={() => setRouteCode("")}>
                ← Back to routes
              </Button>
            </>
          }
        />
      ) : null}

      <Card>
        <div className="flex items-end gap-4 flex-wrap">
          <DatePicker value={date} onChange={setDate} label="Date" />
          <Select
            value={routeCode}
            onChange={setRouteCode}
            options={[
              { value: "", label: "All routes" },
              ...routes.map((r) => ({ value: r, label: r })),
            ]}
            label="Route"
          />
          {!routeCode && (
            <div className="ml-auto">
              <InfoPanel {...RECOMMENDED_ORDERS_INFO} />
            </div>
          )}
        </div>
      </Card>

      {recsError && (
        <div className="bg-danger-50 border border-danger-100 rounded-lg p-4 text-body text-danger-700">
          {recsError}
        </div>
      )}

      {renderBody()}

      <AdoptionDrawer
        open={adoptionOpen}
        onClose={() => setAdoptionOpen(false)}
        routeCode={routeCode || undefined}
      />
      <UpcomingPlanDrawer
        open={upcomingOpen}
        onClose={() => setUpcomingOpen(false)}
        routeCode={routeCode || undefined}
      />
    </div>
  );
}
