import { useMemo, useState } from "react";
import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";
import KpiRow from "@/components/ui/KpiRow";
import { Skeleton } from "@/components/ui/Skeleton";
import PageHeader from "@/components/layout/PageHeader";
import HorizontalBarChart, { type HBarDatum } from "@/components/charts/HorizontalBarChart";
import { CHART_COLOR } from "@/components/charts/theme";
import { useSalesOverview, useBusinessKpis } from "@/hooks/useDataImport";
import { useToast } from "@/hooks/useToast";
import {
  fmtNum,
  fmtCurrency,
  DEFAULT_LOOKBACK,
  DASHBOARD_TOP_N,
  type Lookback,
} from "@/lib/format";
import { fmtDate } from "@/lib/date";
import type { DashboardFilters } from "@/types/data-import";
import { EMPTY_FILTERS } from "@/types/data-import";
import DashboardFilterBar from "./DashboardFilterBar";
import DashboardKpis from "./DashboardKpis";
import DashboardDailyTrend from "./DashboardDailyTrend";

export default function DashboardPage() {
  // Reporting period drives every tile, chart, and table on this page so
  // a single selector controls the whole executive view in lockstep.
  const [lookback, setLookback] = useState<Lookback>(DEFAULT_LOOKBACK);
  // Cascading dashboard filters. Empty arrays === "all" (matches backend).
  const [filters, setFilters] = useState<DashboardFilters>(EMPTY_FILTERS);

  const sales = useSalesOverview(lookback, filters);
  const kpis = useBusinessKpis(lookback, filters);
  const { toast } = useToast();

  const salesData = sales.data?.available ? sales.data : null;
  const k = kpis.data?.available ? kpis.data : null;

  const handleRefresh = () => {
    sales.refetch();
    kpis.refetch();
    toast("Data refreshed", "success");
  };

  // Top routes by revenue: most directly answers "which routes carry the
  // business?" -- the only customer/route question executives ask. Capped
  // at DASHBOARD_TOP_N so the leaderboard stays scannable.
  const topRouteRevenueBars = useMemo<HBarDatum[]>(
    () =>
      (salesData?.top_routes ?? [])
        .slice()
        .sort((a, b) => Number(b.revenue) - Number(a.revenue))
        .slice(0, DASHBOARD_TOP_N)
        .map((r) => ({
          label: `Route ${r.RouteCode}`,
          value: Number(r.revenue ?? 0),
          display: fmtCurrency(r.revenue),
        })),
    [salesData?.top_routes]
  );

  // Categories rendered as a horizontal bar (more legible than a pie when
  // there are >5 slices, and consistent with the route leaderboard beside it).
  const categoryBars = useMemo<HBarDatum[]>(
    () =>
      (salesData?.categories ?? [])
        .slice()
        .sort((a, b) => Number(b.revenue) - Number(a.revenue))
        .slice(0, DASHBOARD_TOP_N)
        .map((c) => ({
          label: c.CategoryName?.trim() || "Uncategorized",
          value: Number(c.revenue ?? 0),
          display: fmtCurrency(c.revenue),
        })),
    [salesData?.categories]
  );

  return (
    <div className="space-y-6">
      <PageHeader
        title="Performance Dashboard"
        subtitle="Track sales, volume, and forecast performance at a glance."
        actions={
          <>
            {k?.anchor_date && (
              <span className="text-caption text-text-tertiary mr-1">
                Data through{" "}
                <span className="font-semibold text-text-secondary">
                  {fmtDate(k.anchor_date)}
                </span>
              </span>
            )}
            <Button variant="ghost" size="sm" onClick={handleRefresh}>
              Refresh
            </Button>
          </>
        }
      />

      <DashboardFilterBar
        value={filters}
        onChange={setFilters}
        lookback={lookback}
        onLookbackChange={setLookback}
      />

      {kpis.loading ? (
        <KpiRow>
          {[1, 2, 3, 4].map((i) => (
            <div key={i} className="bg-surface-sunken rounded-lg p-4 border-l-3 border-neutral-200">
              <Skeleton className="h-3 w-20 mb-2" />
              <Skeleton className="h-6 w-28 mb-1" />
              <Skeleton className="h-3 w-24" />
            </div>
          ))}
        </KpiRow>
      ) : (
        <DashboardKpis k={k} />
      )}

      <DashboardDailyTrend data={salesData} loading={sales.loading} />

      {/* Where the revenue lives: by category and by route, side-by-side. */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card
          title="Revenue by category"
          actions={
            salesData?.categories?.length ? (
              <span className="text-caption text-text-tertiary">
                top {Math.min(salesData.categories.length, DASHBOARD_TOP_N)} of {salesData.categories.length}
              </span>
            ) : undefined
          }
        >
          {sales.loading ? (
            <div className="animate-pulse bg-surface-sunken rounded-lg h-[240px]" />
          ) : (
            <HorizontalBarChart
              data={categoryBars}
              color={CHART_COLOR.info}
              emptyMessage="No category data"
            />
          )}
        </Card>

        <Card
          title="Top routes by revenue"
          actions={
            salesData?.totals?.unique_routes ? (
              <span className="text-caption text-text-tertiary">
                top {Math.min(salesData.totals.unique_routes, DASHBOARD_TOP_N)} of {salesData.totals.unique_routes}
              </span>
            ) : undefined
          }
        >
          {sales.loading ? (
            <div className="animate-pulse bg-surface-sunken rounded-lg h-[240px]" />
          ) : (
            <HorizontalBarChart
              data={topRouteRevenueBars}
              color={CHART_COLOR.brandPrimary}
              emptyMessage="No route data"
            />
          )}
        </Card>
      </div>
    </div>
  );
}
