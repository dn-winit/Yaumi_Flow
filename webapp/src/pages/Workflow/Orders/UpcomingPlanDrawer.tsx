import Drawer from "@/components/ui/Drawer";
import Loading from "@/components/ui/Loading";
import EmptyState from "@/components/ui/EmptyState";
import Card from "@/components/ui/Card";
import Table from "@/components/ui/Table";
import DrawerContextBar from "@/components/ui/DrawerContextBar";
import KpiRow from "@/components/ui/KpiRow";
import MetricCard from "@/components/charts/MetricCard";
import LineChart from "@/components/charts/LineChart";
import { useUpcomingPlan } from "@/hooks/useRecommendedOrder";
import { fmtNum, fmtCurrency } from "@/lib/format";

interface Props {
  open: boolean;
  onClose: () => void;
  routeCode?: string;
  days?: number;
}

const DEFAULT_HORIZON = 7;

export default function UpcomingPlanDrawer({ open, onClose, routeCode, days = DEFAULT_HORIZON }: Props) {
  const params = { days, ...(routeCode ? { route_code: routeCode } : {}) };
  const { data, loading } = useUpcomingPlan(params, open);

  const windowLabel = data
    ? `${data.today} to ${data.daily[data.daily.length - 1]?.date ?? data.today}`
    : "next days";

  const columns = [
    { key: "date", label: "Date", render: (r: Record<string, unknown>) => String(r.date) },
    {
      key: "customers",
      label: "Customers",
      render: (r: Record<string, unknown>) => fmtNum(r.customers as number),
    },
    {
      key: "predicted_qty",
      label: "Predicted qty",
      render: (r: Record<string, unknown>) => fmtNum(r.predicted_qty as number, 1),
    },
    {
      key: "est_revenue",
      label: "Est. revenue",
      render: (r: Record<string, unknown>) =>
        r.est_revenue != null ? fmtCurrency(r.est_revenue as number) : "--",
    },
  ];

  return (
    <Drawer open={open} onClose={onClose} title="Upcoming Week Plan" width="xl">
      <div className="space-y-6">
        <DrawerContextBar
          routeCode={routeCode}
          dateRange={windowLabel}
          extra={
            data ? (
              <span className="text-caption text-text-tertiary">
                {fmtNum(data.summary.active_days)} active days
              </span>
            ) : null
          }
        />

        {loading ? (
          <Loading message="Loading upcoming plan..." />
        ) : !data?.available ? (
          <EmptyState
            icon="📅"
            title="No upcoming data"
            message="Route plan or forecast not available for these days."
          />
        ) : (
          <>
            <KpiRow>
              <MetricCard
                label="Total visits"
                value={fmtNum(data.summary.total_visits)}
                subtitle={`Across ${data.days} days`}
              />
              <MetricCard
                label="Predicted quantity"
                value={fmtNum(data.summary.total_qty, 1)}
                subtitle="Units to load"
              />
              <MetricCard
                label="Estimated revenue"
                value={
                  data.summary.total_revenue != null
                    ? fmtCurrency(data.summary.total_revenue)
                    : "--"
                }
                subtitle={data.summary.total_revenue != null ? "Qty x avg price" : "No price data"}
              />
              <MetricCard
                label="Peak day"
                value={
                  data.summary.peak_day
                    ? fmtNum(data.summary.peak_day.predicted_qty, 1)
                    : "--"
                }
                subtitle={data.summary.peak_day?.date ?? ""}
              />
            </KpiRow>

            <LineChart
              title="Predicted demand by day"
              data={data.daily as unknown as Record<string, unknown>[]}
              xKey="date"
              series={[{ key: "predicted_qty", label: "Predicted qty" }]}
              height={280}
            />

            <Card
              title="Daily breakdown"
              actions={<span className="text-body text-text-tertiary">{data.daily.length} days</span>}
            >
              <Table data={data.daily as unknown as Record<string, unknown>[]} columns={columns} />
            </Card>
          </>
        )}
      </div>
    </Drawer>
  );
}
