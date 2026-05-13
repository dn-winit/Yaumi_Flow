import { useMemo, useState } from "react";
import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";
import KpiRow from "@/components/ui/KpiRow";
import { Skeleton } from "@/components/ui/Skeleton";
import PageHeader from "@/components/layout/PageHeader";
import HorizontalBarChart, { type HBarDatum } from "@/components/charts/HorizontalBarChart";
import { CHART_COLOR } from "@/components/charts/theme";
import { useSalesOverview, useBusinessKpis, useLastActiveDate } from "@/hooks/useDataImport";
import { useToast } from "@/hooks/useToast";
import { fmtCurrency, DASHBOARD_TOP_N } from "@/lib/format";
import { defaultReportingPeriod, fmtDate } from "@/lib/date";
import type { DashboardFilters, ReportingPeriod } from "@/types/data-import";
import { EMPTY_FILTERS } from "@/types/data-import";
import DashboardFilterBar from "./DashboardFilterBar";
import DashboardKpis from "./DashboardKpis";
import DashboardDailyTrend from "./DashboardDailyTrend";

export default function DashboardPage() {
  // Reporting period drives every tile, chart, and table on this page so
  // a single picker controls the whole executive view in lockstep.
  const [period, setPeriod] = useState<ReportingPeriod>(() => defaultReportingPeriod());
  // Cascading dashboard filters. Empty arrays === "all" (matches backend).
  const [filters, setFilters] = useState<DashboardFilters>(EMPTY_FILTERS);

  const sales = useSalesOverview(period, filters);
  const kpis = useBusinessKpis(period, filters);
  // lastActiveDate surfaces in the empty-state hint so the user has a
  // concrete "try this date instead" anchor when they land on a weekend
  // or holiday with zero rows. Static-tier cached so it costs nothing.
  const { date: lastActiveDate } = useLastActiveDate();
  const { toast } = useToast();

  const salesData = sales.data?.available ? sales.data : null;
  const k = kpis.data?.available ? kpis.data : null;
  // Transparent no-data signal: the backend returned ``available: true``
  // but the window had zero activity in scope -- almost always a
  // weekend / holiday pick. Compute once here so the banner, KPI grid,
  // and chart all read from the same flag.
  const noActivityInWindow =
    sales.data?.available === true &&
    !sales.loading &&
    (salesData?.totals?.working_days ?? 0) === 0;
  const singleDay = period.start_date === period.end_date;

  const handleRefresh = () => {
    sales.refetch();
    kpis.refetch();
    toast("Data refreshed", "success");
  };

  // Top routes / categories arrive already sorted by revenue desc from
  // the backend (single source of truth). We just take the leaderboard
  // cap so the cards stay scannable -- no re-sort here.
  const topRouteRevenueBars = useMemo<HBarDatum[]>(
    () =>
      (salesData?.top_routes ?? [])
        .slice(0, DASHBOARD_TOP_N)
        .map((r) => ({
          label: `Route ${r.RouteCode}`,
          value: Number(r.revenue ?? 0),
          display: fmtCurrency(r.revenue),
        })),
    [salesData?.top_routes]
  );

  const categoryBars = useMemo<HBarDatum[]>(
    () =>
      (salesData?.categories ?? [])
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
        title="Sales performance & forecast impact"
        subtitle="What actually sold across the business, and how our forecast performed against it."
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
        period={period}
        onPeriodChange={setPeriod}
      />

      {noActivityInWindow ? (
        <Card>
          <div className="px-2 py-6 text-center space-y-2">
            <div className="text-h3 font-semibold text-text-primary">
              {singleDay
                ? `No sales activity on ${fmtDate(period.end_date)}`
                : `No sales activity from ${fmtDate(period.start_date)} to ${fmtDate(period.end_date)}`}
            </div>
            <div className="text-body text-text-secondary">
              {singleDay
                ? "This date is most likely a weekend, holiday, or other closure. The dashboard has no rows to summarise for it."
                : "Every day in the chosen range has zero rows in scope. The dashboard has nothing to summarise."}
            </div>
            {lastActiveDate && lastActiveDate !== period.end_date && (
              <div className="text-caption text-text-tertiary pt-2">
                The most recent date with activity is{" "}
                <span className="font-semibold text-text-secondary">
                  {fmtDate(lastActiveDate)}
                </span>
                . Adjust the reporting period to see data again.
              </div>
            )}
          </div>
        </Card>
      ) : (
        <>
          {kpis.loading ? (
            <div className="space-y-3">
              <Skeleton className="h-3 w-44" />
              <KpiRow>
                {[1, 2, 3].map((i) => (
                  <div key={i} className="bg-surface-sunken rounded-lg p-4 border-l-3 border-brand-200">
                    <Skeleton className="h-3 w-20 mb-2" />
                    <Skeleton className="h-6 w-28 mb-1" />
                    <Skeleton className="h-3 w-24" />
                  </div>
                ))}
              </KpiRow>
            </div>
          ) : (
            <DashboardKpis k={k} />
          )}

          <DashboardDailyTrend data={salesData} loading={sales.loading} />

          {/* Where the revenue lives: by category and by route, side-by-side. */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
            <Card
              title="Revenue by category"
              actions={
                salesData?.totals?.unique_categories ? (
                  <span className="text-caption text-text-tertiary">
                    top {Math.min(salesData.totals.unique_categories, DASHBOARD_TOP_N)} of {salesData.totals.unique_categories}
                  </span>
                ) : undefined
              }
            >
              {sales.loading ? (
                <Skeleton className="h-[240px] rounded-lg" />
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
                <Skeleton className="h-[240px] rounded-lg" />
              ) : (
                <HorizontalBarChart
                  data={topRouteRevenueBars}
                  color={CHART_COLOR.brandPrimary}
                  emptyMessage="No route data"
                />
              )}
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
