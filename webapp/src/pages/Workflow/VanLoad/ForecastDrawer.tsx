import Drawer from "@/components/ui/Drawer";
import Loading from "@/components/ui/Loading";
import EmptyState from "@/components/ui/EmptyState";
import Card from "@/components/ui/Card";
import Table from "@/components/ui/Table";
import DrawerContextBar from "@/components/ui/DrawerContextBar";
import MetricCard from "@/components/charts/MetricCard";
import LineChart from "@/components/charts/LineChart";
import { CHART_COLOR } from "@/components/charts/theme";
import PredictedValue from "@/components/ui/PredictedValue";
import ConfidenceBadge from "@/components/ui/ConfidenceBadge";
import { useForecastDrawerPageView } from "@/hooks/useForecast";
import { fmtNum } from "@/lib/format";
import { fmtDate, fmtDateRange, todayIso } from "@/lib/date";
import type { ForecastDrawerTableRow } from "@/api/forecast";
import type { Row } from "@/types/common";

interface Props {
  open: boolean;
  onClose: () => void;
  routeCode?: string;
  itemCodes?: string[];
}

/** Adapter: wrap a ForecastDrawerTableRow into the legacy Row shape
 *  the ExplainabilityModal reads. The server already supplies the
 *  engine intermediates the modal renders inside ``explain``. */
function toLegacyRow(r: ForecastDrawerTableRow, routeCode: string | undefined): Row {
  return {
    ItemCode: r.item_code,
    ItemName: r.item_name,
    RouteCode: routeCode ?? "",
    TrxDate: r.date,
    prediction: r.units_to_load,
    p_demand: r.p_demand,
    demand_class: r.demand_class,
    class: r.demand_class,
    lower_bound: r.lower_bound,
    upper_bound: r.upper_bound,
    ...r.explain,
  } as unknown as Row;
}

export default function ForecastDrawer({ open, onClose, routeCode, itemCodes }: Props) {
  const { data: view, loading } = useForecastDrawerPageView(
    routeCode,
    itemCodes,
    todayIso(),
    open,
  );

  const showBand = Boolean(view?.show_band);

  const columns = [
    {
      key: "Date",
      label: "Date",
      render: (r: ForecastDrawerTableRow) => fmtDate(r.date),
    },
    {
      key: "ItemCode",
      label: "Item",
      render: (r: ForecastDrawerTableRow) => r.item_code || "-",
    },
    {
      key: "Predicted",
      label: "Units to load",
      render: (r: ForecastDrawerTableRow) => (
        <PredictedValue
          row={toLegacyRow(r, routeCode)}
          value={r.units_to_load}
        />
      ),
    },
    {
      key: "Confidence",
      label: "Chance of selling",
      render: (r: ForecastDrawerTableRow) => (
        <ConfidenceBadge
          value={r.p_demand}
          demandClass={r.demand_class ?? undefined}
        />
      ),
    },
    {
      key: "Range",
      label: "Likely range (low-high)",
      render: (r: ForecastDrawerTableRow) => {
        if (r.lower_bound == null || r.upper_bound == null) return "-";
        return `${fmtNum(r.lower_bound)} - ${fmtNum(r.upper_bound)}`;
      },
    },
  ];

  const summary = view?.summary;
  const windowLabel =
    summary && summary.window_start && summary.window_end
      ? fmtDateRange(summary.window_start, summary.window_end)
      : `from ${fmtDate(todayIso())}`;

  return (
    <Drawer open={open} onClose={onClose} title="Upcoming plan - van load" width="xl">
      <div className="space-y-6">
        <DrawerContextBar
          routeCode={routeCode}
          itemCodes={itemCodes}
          dateRange={windowLabel}
          extra={
            summary && summary.line_count > 0 ? (
              <span className="text-caption text-text-tertiary">
                {fmtNum(summary.line_count)} lines
              </span>
            ) : undefined
          }
        />

        {loading ? (
          <Loading message="Loading upcoming van load..." />
        ) : !view || !view.available || !summary || view.table_rows.length === 0 ? (
          <EmptyState
            title="Nothing scheduled"
            message={
              view?.message ||
              "No upcoming load from today onwards for this route/item selection."
            }
            icon="trend"
          />
        ) : (
          <>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <MetricCard
                label="Days covered"
                value={`${summary.horizon_days} days`}
                subtitle={`${summary.skus} items`}
              />
              <MetricCard
                label="Total van load"
                value={fmtNum(summary.total_van_load)}
                subtitle="Units across the window"
              />
              <MetricCard
                label="Avg per day"
                value={summary.horizon_days > 0 ? fmtNum(summary.avg_per_day) : "-"}
                subtitle="Units / day"
              />
            </div>

            <LineChart
              title={`Daily van load${showBand ? "" : " - route total"}`}
              data={view.chart_data as unknown as Record<string, unknown>[]}
              xKey="date"
              series={
                showBand
                  ? [
                      { key: "q90", label: "Best case", color: CHART_COLOR.brandBand },
                      { key: "predicted", label: "Van load", color: CHART_COLOR.brandPrimary },
                      { key: "q10", label: "Worst case", color: CHART_COLOR.brandBand },
                    ]
                  : [{ key: "predicted", label: "Van load" }]
              }
              height={320}
            />

            <Card
              title="Line-item detail"
              actions={
                <span className="text-body text-text-tertiary">
                  {view.table_rows.length} rows
                </span>
              }
            >
              <Table
                data={view.table_rows as unknown as Record<string, unknown>[]}
                columns={columns as unknown as Parameters<typeof Table>[0]["columns"]}
              />
            </Card>
          </>
        )}
      </div>
    </Drawer>
  );
}
