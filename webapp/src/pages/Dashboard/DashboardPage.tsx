import { useMemo, useState } from "react";
import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";
import KpiRow from "@/components/ui/KpiRow";
import { Skeleton } from "@/components/ui/Skeleton";
import EmptyState from "@/components/ui/EmptyState";
import PageHeader from "@/components/layout/PageHeader";
import MetricCard from "@/components/charts/MetricCard";
import LineChart from "@/components/charts/LineChart";
import BarChart from "@/components/charts/BarChart";
import PieChart from "@/components/charts/PieChart";
import HorizontalBarChart, { type HBarDatum } from "@/components/charts/HorizontalBarChart";
import { CHART_COLOR } from "@/components/charts/theme";
import { useSalesOverview, useCustomerOverview, useBusinessKpis } from "@/hooks/useDataImport";
import { useToast } from "@/hooks/useToast";
import { useRetrainConfig } from "@/hooks/useForecast";
import {
  fmtNum,
  fmtCurrency,
  fmtDelta,
  GOOD_SCORE_THRESHOLD,
  DASHBOARD_WINDOWS,
  DEFAULT_DASHBOARD_WINDOW,
  DASHBOARD_TOP_N,
  type DashboardWindow,
} from "@/lib/format";
import type { BusinessKpis } from "@/types/data-import";
import type { DriftStatus } from "@/types/forecast";

function toneToTrend(tone: "up" | "down" | "flat"): "up" | "down" | undefined {
  if (tone === "up") return "up";
  if (tone === "down") return "down";
  return undefined;
}

function DashboardKpis({ k, drift }: { k: BusinessKpis | null; drift?: DriftStatus | null }) {
  const yDelta = fmtDelta(k?.yesterday?.delta_pct_vs_last_week);
  const wDelta = fmtDelta(k?.last_7_days?.delta_pct_vs_prior_7d);
  // Use live accuracy from drift detection (same source as Pipeline page)
  // so the number is consistent everywhere. Falls back to CSV-based accuracy
  // when the forecast service is unavailable.
  const accuracy = drift?.recent_accuracy ?? k?.forecast_accuracy_7d?.accuracy_pct ?? null;
  const ops = k?.today_operations;

  return (
    <KpiRow>
      <MetricCard
        label="Yesterday revenue"
        value={fmtCurrency(k?.yesterday?.revenue)}
        subtitle={`${yDelta.text} ${k?.yesterday?.comparison_label ?? ""}`}
        trend={toneToTrend(yDelta.tone)}
      />
      <MetricCard
        label="Last 7 days revenue"
        value={fmtCurrency(k?.last_7_days?.revenue)}
        subtitle={`${wDelta.text} vs prior 7d`}
        trend={toneToTrend(wDelta.tone)}
      />
      <MetricCard
        label="Forecast accuracy (7d)"
        value={accuracy != null ? `${accuracy.toFixed(1)}%` : "--"}
        subtitle={
          drift?.baseline_accuracy != null
            ? `vs ${drift.baseline_accuracy.toFixed(1)}% at training`
            : "live predictions vs actual sales"
        }
        trend={
          accuracy == null
            ? undefined
            : drift?.baseline_accuracy != null
            ? accuracy >= drift.baseline_accuracy ? "up" : "down"
            : accuracy >= GOOD_SCORE_THRESHOLD ? "up" : "down"
        }
      />
      <MetricCard
        label="Operations (7d)"
        value={`${ops?.routes ?? 0} routes`}
        subtitle={`${fmtNum(ops?.customers)} customers planned · ${ops?.days_active ?? 0} active days`}
      />
    </KpiRow>
  );
}

export default function DashboardPage() {
  // Single window state drives every chart + table below the KPI row.
  // KPI tiles keep their own fixed windows because those are part of the
  // metric definition (e.g. "yesterday revenue" can't have a longer window).
  const [windowDays, setWindowDays] = useState<DashboardWindow>(DEFAULT_DASHBOARD_WINDOW);

  const sales = useSalesOverview(windowDays);
  const customers = useCustomerOverview(windowDays);
  const kpis = useBusinessKpis();
  const { data: retrainConfig } = useRetrainConfig();
  const { toast } = useToast();

  const salesData = sales.data?.available ? sales.data : null;
  const customerData = customers.data?.available ? customers.data : null;
  const k = kpis.data?.available ? kpis.data : null;

  const handleRefresh = () => {
    sales.refetch();
    customers.refetch();
    kpis.refetch();
    toast("Data refreshed", "success");
  };

  // Shape backend rows into the horizontal-bar datum the chart wants. Each
  // chart sorts descending and caps at DASHBOARD_TOP_N so the leader board
  // stays scannable; backend payload still has the full top-10 should the
  // cap ever change.
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

  const topRouteCustomerBars = useMemo<HBarDatum[]>(
    () =>
      (customerData?.by_route ?? [])
        .slice()
        .sort((a, b) => Number(b.customers) - Number(a.customers))
        .slice(0, DASHBOARD_TOP_N)
        .map((r) => ({
          label: `Route ${r.route_code}`,
          value: Number(r.customers ?? 0),
          display: `${fmtNum(r.customers)} cust`,
        })),
    [customerData?.by_route]
  );

  const topCustomerBars = useMemo<HBarDatum[]>(
    () =>
      (customerData?.top_customers ?? [])
        .slice()
        .sort((a, b) => Number(b.total_quantity) - Number(a.total_quantity))
        .slice(0, DASHBOARD_TOP_N)
        .map((c) => {
          const name = c.customer_name?.trim() || c.customer_code;
          // Trim long names so y-axis labels stay readable in the fixed width.
          const label = name.length > 22 ? `${name.slice(0, 21)}…` : name;
          return {
            label,
            value: Number(c.total_quantity ?? 0),
            display: `${fmtNum(c.total_quantity)} units`,
          };
        }),
    [customerData?.top_customers]
  );

  // Section 3 view toggle -- "by revenue" vs "by customers". Single piece of
  // state, declarative -- no tab framework needed for two buttons.
  const [routeView, setRouteView] = useState<"revenue" | "customers">("revenue");

  return (
    <div className="space-y-6">
      <PageHeader
        title="Dashboard"
        subtitle={
          k?.anchor_date
            ? `Operations pulse — data through ${k.anchor_date}`
            : "Operations pulse"
        }
        actions={
          <Button variant="ghost" size="sm" onClick={handleRefresh}>
            Refresh
          </Button>
        }
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
        <DashboardKpis k={k} drift={retrainConfig?.drift} />
      )}

      {/* Window selector — drives every chart + table below. */}
      <div className="flex items-end justify-between gap-4 flex-wrap pt-2 border-t border-subtle">
        <div className="pt-4">
          <h2 className="text-h3 font-semibold text-text-primary">
            Trends &amp; breakdowns
          </h2>
          <p className="text-caption text-text-tertiary mt-0.5">
            {salesData?.totals
              ? `${fmtNum(salesData.totals.transactions)} transactions · ${fmtCurrency(salesData.totals.total_revenue)} · ${salesData.totals.first_date} → ${salesData.totals.last_date}`
              : `Last ${windowDays} days`}
          </p>
        </div>
        <WindowPills active={windowDays} onChange={setWindowDays} />
      </div>

      {/* Section 1 — the pulse: full-width sales trend */}
      <Card title="Daily Sales Trend">
        {sales.loading ? (
          <div className="animate-pulse bg-surface-sunken rounded-lg h-[300px]" />
        ) : !salesData?.daily_trend || salesData.daily_trend.length === 0 ? (
          <EmptyState title="No trend data" />
        ) : (
          <LineChart
            data={salesData.daily_trend as unknown as Record<string, unknown>[]}
            xKey="date"
            series={[
              { key: "quantity", label: "Quantity" },
              { key: "revenue", label: "Revenue" },
            ]}
            height={300}
          />
        )}
      </Card>

      {/* Section 2 — what's selling: top items + category split, side-by-side */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card
          title={`Top ${DASHBOARD_TOP_N} Items by Quantity`}
          actions={
            salesData?.top_items?.length ? (
              <span className="text-caption text-text-tertiary">
                {fmtNum(salesData.totals?.unique_items)} SKUs in window
              </span>
            ) : undefined
          }
        >
          {sales.loading ? (
            <div className="animate-pulse bg-surface-sunken rounded-lg h-[240px]" />
          ) : !salesData?.top_items || salesData.top_items.length === 0 ? (
            <EmptyState title="No data" />
          ) : (
            <BarChart
              data={salesData.top_items.slice(0, DASHBOARD_TOP_N) as unknown as Record<string, unknown>[]}
              xKey="ItemCode"
              yKey="quantity"
              height={240}
            />
          )}
        </Card>

        <Card
          title="Sales by Category"
          actions={
            salesData?.categories?.length ? (
              <span className="text-caption text-text-tertiary">
                {salesData.categories.length} categories
              </span>
            ) : undefined
          }
        >
          {sales.loading ? (
            <div className="animate-pulse bg-surface-sunken rounded-lg h-[240px]" />
          ) : !salesData?.categories || salesData.categories.length === 0 ? (
            <EmptyState title="No data" />
          ) : (
            <PieChart
              data={salesData.categories.map((c) => ({
                name: c.CategoryName || "Other",
                value: c.quantity,
              }))}
              dataKey="value"
              nameKey="name"
              height={240}
            />
          )}
        </Card>
      </div>

      {/* Section 3 — leaderboards: routes (tabbed) + customers, side-by-side.
          Tabs collapse the two route perspectives into a single card so the
          page stays scannable; the customer leaderboard sits right alongside. */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card
          title="Route Insights"
          actions={
            <RouteViewToggle active={routeView} onChange={setRouteView} />
          }
        >
          {(routeView === "revenue" ? sales.loading : customers.loading) ? (
            <div className="animate-pulse bg-surface-sunken rounded-lg h-[240px]" />
          ) : routeView === "revenue" ? (
            <HorizontalBarChart
              data={topRouteRevenueBars}
              color={CHART_COLOR.brandPrimary}
              emptyMessage="No route data"
            />
          ) : (
            <HorizontalBarChart
              data={topRouteCustomerBars}
              color={CHART_COLOR.info}
              emptyMessage="No route data"
            />
          )}
        </Card>

        <Card
          title={`Top ${DASHBOARD_TOP_N} Customers`}
          actions={
            customerData?.totals ? (
              <span className="text-caption text-text-tertiary">
                of {fmtNum(customerData.totals.active_customers)} who bought
              </span>
            ) : undefined
          }
        >
          {customers.loading ? (
            <div className="animate-pulse bg-surface-sunken rounded-lg h-[240px]" />
          ) : (
            <HorizontalBarChart
              data={topCustomerBars}
              color={CHART_COLOR.success}
              labelWidth={150}
              emptyMessage="No customer data available"
            />
          )}
        </Card>
      </div>
    </div>
  );
}

function WindowPills({
  active,
  onChange,
}: {
  active: DashboardWindow;
  onChange: (w: DashboardWindow) => void;
}) {
  return (
    <div className="inline-flex items-center rounded-lg border border-default bg-surface-base p-1 gap-0.5">
      {DASHBOARD_WINDOWS.map((d) => {
        const isActive = d === active;
        return (
          <button
            key={d}
            type="button"
            onClick={() => onChange(d)}
            aria-pressed={isActive}
            className={[
              "px-3 py-1.5 text-caption font-semibold rounded-md transition-all duration-base whitespace-nowrap",
              isActive
                ? "bg-brand-600 text-white shadow-sm"
                : "text-text-secondary hover:text-text-primary hover:bg-surface-sunken",
            ].join(" ")}
          >
            {d === 7 ? "7 days" : `${d} days`}
          </button>
        );
      })}
    </div>
  );
}

const ROUTE_VIEW_OPTIONS = [
  { key: "revenue" as const, label: "By revenue" },
  { key: "customers" as const, label: "By customers" },
];

function RouteViewToggle({
  active,
  onChange,
}: {
  active: "revenue" | "customers";
  onChange: (v: "revenue" | "customers") => void;
}) {
  return (
    <div className="inline-flex items-center rounded-md border border-default bg-surface-base p-0.5 gap-0.5">
      {ROUTE_VIEW_OPTIONS.map((opt) => {
        const isActive = opt.key === active;
        return (
          <button
            key={opt.key}
            type="button"
            onClick={() => onChange(opt.key)}
            aria-pressed={isActive}
            className={[
              "px-2.5 py-1 text-caption font-semibold rounded transition-all duration-base whitespace-nowrap",
              isActive
                ? "bg-brand-600 text-white"
                : "text-text-secondary hover:text-text-primary hover:bg-surface-sunken",
            ].join(" ")}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
