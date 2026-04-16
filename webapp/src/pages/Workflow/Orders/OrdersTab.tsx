import { useMemo, useState } from "react";
import Card from "@/components/ui/Card";
import Button from "@/components/ui/Button";
import ContextStrip from "@/components/ui/ContextStrip";
import DatePicker from "@/components/ui/DatePicker";
import Select from "@/components/ui/Select";
import Loading from "@/components/ui/Loading";
import EmptyState from "@/components/ui/EmptyState";
import { useFilterOptions, useRecommendations, useGenerate } from "@/hooks/useRecommendedOrder";
import RoutePickerGrid from "@/pages/RecommendedOrders/RoutePickerGrid";
import CustomerRecommendationsPanel from "@/pages/RecommendedOrders/CustomerRecommendationsPanel";

import AdoptionDrawer from "./AdoptionDrawer";
import UpcomingPlanDrawer from "./UpcomingPlanDrawer";
import { useWorkflow } from "../workflowContext";

export default function OrdersTab() {
  const { date, setDate } = useWorkflow();
  // Route selection is tab-local so leaving and returning to Orders always
  // starts at the route-picker grid.
  const [routeCode, setRouteCode] = useState("");

  const { data: filterOptions, loading: optionsLoading } = useFilterOptions();

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

  const handleGenerateRoute = async () => {
    if (!routeCode) return;
    await generate(date, [routeCode], true);
    refetchRecs();
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
          onRouteSelect={setRouteCode}
          onGenerated={refetchRecs}
        />
      );
    }

    if (!hasData) {
      return (
        <Card>
          <EmptyState
            icon="📦"
            title="No recommendations yet"
            message={`No recommendations for route ${routeCode} on ${date}. Auto-generation runs nightly -- or trigger it manually now.`}
            action={
              <div className="flex flex-col items-center gap-2">
                <Button variant="primary" loading={generating} onClick={handleGenerateRoute}>
                  Generate for route {routeCode}
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
      {/* Status banner */}
      {routeCode && recsData?.data?.length ? (
        <ContextStrip
          items={[
            { label: "Route", value: routeCode },
            { label: "Recommendations", value: recsData.total },
            { label: "Date", value: date },
            ...(justGenerated
              ? [{ label: "Status", value: "just generated" }]
              : []),
          ]}
          actions={
            <Button variant="ghost" size="sm" onClick={() => setRouteCode("")}>
              &larr; Back to routes
            </Button>
          }
        />
      ) : null}

      {/* Filters */}
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
        </div>
      </Card>

      {recsError && (
        <div className="bg-danger-50 border border-danger-100 rounded-lg p-4 text-body text-danger-700">
          {recsError}
        </div>
      )}

      {/* Analytics drawers -- historical adoption + upcoming plan */}
      <div className="flex flex-wrap gap-3">
        <Button variant="secondary" onClick={() => setAdoptionOpen(true)}>
          Last 30 Days Adoption
        </Button>
        <Button variant="secondary" onClick={() => setUpcomingOpen(true)}>
          Upcoming Week Plan
        </Button>
      </div>

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
