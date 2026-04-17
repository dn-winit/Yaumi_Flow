import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";
import KpiRow from "@/components/ui/KpiRow";
import { Skeleton } from "@/components/ui/Skeleton";
import Table from "@/components/ui/Table";
import EmptyState from "@/components/ui/EmptyState";
import PageHeader from "@/components/layout/PageHeader";
import MetricCard from "@/components/charts/MetricCard";
import LineChart from "@/components/charts/LineChart";
import BarChart from "@/components/charts/BarChart";
import PieChart from "@/components/charts/PieChart";
import { useSalesOverview, useCustomerOverview, useBusinessKpis } from "@/hooks/useDataImport";
import { useToast } from "@/hooks/useToast";
import { useRetrainConfig } from "@/hooks/useForecast";
import { fmtNum, fmtCurrency, fmtDelta, GOOD_SCORE_THRESHOLD } from "@/lib/format";
import type { BusinessKpis } from "@/types/data-import";
import type { DriftStatus } from "@/types/forecast";

const CUSTOMER_LOOKBACK_DAYS = 90;

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
        subtitle={`${fmtNum(ops?.customers)} customers · ${ops?.days_active ?? 0} active days`}
      />
    </KpiRow>
  );
}

export default function DashboardPage() {
  const sales = useSalesOverview();
  const customers = useCustomerOverview(CUSTOMER_LOOKBACK_DAYS);
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

      <Card title="Daily Sales Trend (last 90 days)">
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

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card title="Top 10 Items by Quantity">
          {sales.loading ? (
            <div className="animate-pulse bg-surface-sunken rounded-lg h-[280px]" />
          ) : !salesData?.top_items || salesData.top_items.length === 0 ? (
            <EmptyState title="No data" />
          ) : (
            <BarChart
              data={salesData.top_items as unknown as Record<string, unknown>[]}
              xKey="ItemCode"
              yKey="quantity"
              height={280}
            />
          )}
        </Card>

        <Card title="Sales by Category">
          {sales.loading ? (
            <div className="animate-pulse bg-surface-sunken rounded-lg h-[280px]" />
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
              height={280}
            />
          )}
        </Card>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        <Card title="Top Routes by Quantity">
          {sales.loading ? (
            <div className="space-y-2">
              {[1, 2, 3, 4, 5].map((i) => <Skeleton key={i} className="h-10 w-full rounded" />)}
            </div>
          ) : !salesData?.top_routes || salesData.top_routes.length === 0 ? (
            <EmptyState title="No data" />
          ) : (
            <Table
              data={salesData.top_routes as unknown as Record<string, unknown>[]}
              columns={[
                { key: "RouteCode", label: "Route" },
                { key: "quantity", label: "Quantity", sortable: true, align: "right", render: (r) => fmtNum(Number(r.quantity)) },
                { key: "revenue", label: "Revenue", sortable: true, align: "right", render: (r) => fmtCurrency(Number(r.revenue)) },
                { key: "items", label: "Items" },
              ]}
            />
          )}
        </Card>

        <Card title={`Top Customers (last ${CUSTOMER_LOOKBACK_DAYS} days)`}>
          {customers.loading ? (
            <div className="space-y-2">
              {[1, 2, 3, 4, 5].map((i) => <Skeleton key={i} className="h-10 w-full rounded" />)}
            </div>
          ) : !customerData?.top_customers || customerData.top_customers.length === 0 ? (
            <EmptyState title="No customer data available" />
          ) : (
            <Table
              data={customerData.top_customers as unknown as Record<string, unknown>[]}
              columns={[
                { key: "customer_code", label: "Code" },
                {
                  key: "customer_name",
                  label: "Name",
                  render: (r) => (
                    <span className="truncate">{String(r.customer_name ?? "").slice(0, 30)}</span>
                  ),
                },
                { key: "visits", label: "Visits", sortable: true, align: "right" },
                {
                  key: "total_quantity",
                  label: "Qty",
                  sortable: true,
                  align: "right",
                  render: (r) => fmtNum(Number(r.total_quantity)),
                },
                { key: "last_purchase", label: "Last" },
              ]}
            />
          )}
        </Card>
      </div>

      {customerData?.by_route && customerData.by_route.length > 0 && (
        <Card title={`Customers per Route (last ${CUSTOMER_LOOKBACK_DAYS} days)`}>
          <BarChart
            data={customerData.by_route as unknown as Record<string, unknown>[]}
            xKey="route_code"
            yKey="customers"
            height={240}
          />
        </Card>
      )}
    </div>
  );
}
