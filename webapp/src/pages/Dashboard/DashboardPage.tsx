import Button from "@/components/ui/Button";
import Card from "@/components/ui/Card";
import KpiRow from "@/components/ui/KpiRow";
import Loading from "@/components/ui/Loading";
import Table from "@/components/ui/Table";
import EmptyState from "@/components/ui/EmptyState";
import PageHeader from "@/components/layout/PageHeader";
import MetricCard from "@/components/charts/MetricCard";
import LineChart from "@/components/charts/LineChart";
import BarChart from "@/components/charts/BarChart";
import PieChart from "@/components/charts/PieChart";

import ServiceHealthCard from "./ServiceHealthCard";
import { useHealth } from "@/hooks/useHealth";
import { useSalesOverview, useCustomerOverview, useBusinessKpis } from "@/hooks/useDataImport";
import { fmtNum, fmtCurrency, fmtDelta, GOOD_SCORE_THRESHOLD } from "@/lib/format";
import type { BusinessKpis } from "@/types/data-import";

const CUSTOMER_LOOKBACK_DAYS = 90;

function toneToTrend(tone: "up" | "down" | "flat"): "up" | "down" | undefined {
  if (tone === "up") return "up";
  if (tone === "down") return "down";
  return undefined;
}

function DashboardKpis({ k }: { k: BusinessKpis | null }) {
  const yDelta = fmtDelta(k?.yesterday?.delta_pct_vs_last_week);
  const wDelta = fmtDelta(k?.last_7_days?.delta_pct_vs_prior_7d);
  const accuracy = k?.forecast_accuracy_7d?.accuracy_pct;
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
          k?.forecast_accuracy_7d?.rows_compared
            ? `${fmtNum(k.forecast_accuracy_7d.rows_compared)} recommendations checked`
            : "nothing to compare yet"
        }
        trend={accuracy != null && accuracy >= GOOD_SCORE_THRESHOLD ? "up" : "down"}
      />
      <MetricCard
        label="Today's operations"
        value={`${ops?.routes ?? 0} routes`}
        subtitle={`${fmtNum(ops?.customers)} customers planned`}
      />
    </KpiRow>
  );
}

export default function DashboardPage() {
  const health = useHealth();
  const sales = useSalesOverview();
  const customers = useCustomerOverview(CUSTOMER_LOOKBACK_DAYS);
  const kpis = useBusinessKpis();

  const salesData = sales.data?.available ? sales.data : null;
  const customerData = customers.data?.available ? customers.data : null;
  const k = kpis.data?.available ? kpis.data : null;

  const handleRefresh = () => {
    sales.refetch();
    customers.refetch();
    kpis.refetch();
    health.refetch();
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="Dashboard"
        subtitle={
          k?.anchor_date
            ? `Operations pulse - data through ${k.anchor_date}`
            : "Operations pulse"
        }
        actions={
          <Button variant="ghost" size="sm" onClick={handleRefresh}>
            Refresh
          </Button>
        }
      />

      {kpis.loading ? <Loading message="Loading KPIs..." /> : <DashboardKpis k={k} />}

      <Card title="Daily Sales Trend (last 90 days)">
        {sales.loading ? (
          <Loading />
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
            <Loading />
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
            <Loading />
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
            <Loading />
          ) : !salesData?.top_routes || salesData.top_routes.length === 0 ? (
            <EmptyState title="No data" />
          ) : (
            <Table
              data={salesData.top_routes as unknown as Record<string, unknown>[]}
              columns={[
                { key: "RouteCode", label: "Route" },
                { key: "quantity", label: "Quantity", render: (r) => fmtNum(Number(r.quantity)) },
                { key: "revenue", label: "Revenue", render: (r) => fmtCurrency(Number(r.revenue)) },
                { key: "items", label: "Items" },
              ]}
            />
          )}
        </Card>

        <Card title={`Top Customers (last ${CUSTOMER_LOOKBACK_DAYS} days)`}>
          {customers.loading ? (
            <Loading />
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
                { key: "visits", label: "Visits" },
                {
                  key: "total_quantity",
                  label: "Qty",
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

      <Card
        title="Service Health"
        actions={
          <span className="text-sm text-text-tertiary">
            {health.loading
              ? "Checking..."
              : `${health.services.filter((s) => s.ok).length}/${health.services.length} up`}
          </span>
        }
      >
        {health.loading ? (
          <Loading message="Checking services..." />
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
            {health.services.map((s) => (
              <ServiceHealthCard
                key={s.service}
                service={s.service}
                status={s.status}
                ok={s.ok}
              />
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
