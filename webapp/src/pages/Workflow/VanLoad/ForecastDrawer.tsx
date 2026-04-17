import { useMemo } from "react";
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
import { useFutureForecast } from "@/hooks/useForecast";
import { toNum, pickDate, fmtNum } from "@/lib/format";
import { todayIso } from "@/lib/date";
import type { Row } from "@/types/common";

interface Props {
  open: boolean;
  onClose: () => void;
  routeCode?: string;
  itemCodes?: string[];
}

export default function ForecastDrawer({ open, onClose, routeCode, itemCodes }: Props) {
  const params = useMemo(() => {
    const p: Record<string, unknown> = {};
    if (routeCode) p.route_code = routeCode;
    if (itemCodes && itemCodes.length === 1) p.item_code = itemCodes[0];
    return p;
  }, [routeCode, itemCodes]);

  const { data, loading } = useFutureForecast(params, open);

  // "Future Forecast" is strictly forward-looking.
  const today = todayIso();
  const filteredRows = useMemo(() => {
    const rows = (data?.data ?? []) as Row[];
    const future = rows.filter((r) => pickDate(r) >= today);
    if (!itemCodes || itemCodes.length === 0) return future;
    const set = new Set(itemCodes);
    return future.filter((r) => set.has(String(r.ItemCode ?? "")));
  }, [data, itemCodes, today]);

  // Quantiles are not additive across SKUs -- only render the band when exactly
  // one SKU is in view.
  const showBand =
    filteredRows.length > 0 &&
    new Set(filteredRows.map((r) => String(r.ItemCode ?? ""))).size === 1;

  const chartData = useMemo(() => {
    const map = new Map<string, { predicted: number; q10: number; q90: number }>();
    filteredRows.forEach((r) => {
      const d = pickDate(r);
      if (!d) return;
      const cur = map.get(d) ?? { predicted: 0, q10: 0, q90: 0 };
      cur.predicted += toNum(r.prediction) ?? 0;
      if (showBand) {
        cur.q10 += toNum(r.q_10) ?? 0;
        cur.q90 += toNum(r.q_90) ?? 0;
      }
      map.set(d, cur);
    });
    return Array.from(map.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([date, v]) => ({
        date,
        predicted: Number(v.predicted.toFixed(2)),
        q10: Number(v.q10.toFixed(2)),
        q90: Number(v.q90.toFixed(2)),
      }));
  }, [filteredRows, showBand]);

  const summary = useMemo(() => {
    if (chartData.length === 0) {
      return { horizon: 0, total: 0, skus: 0 };
    }
    const total = chartData.reduce((n, r) => n + r.predicted, 0);
    const skus = new Set(filteredRows.map((r) => String(r.ItemCode ?? ""))).size;
    return { horizon: chartData.length, total, skus };
  }, [chartData, filteredRows]);

  const tableRows = useMemo(() => {
    return [...filteredRows].sort((a, b) => {
      const da = pickDate(a);
      const db = pickDate(b);
      if (da !== db) return da.localeCompare(db);
      return (toNum(b.prediction) ?? 0) - (toNum(a.prediction) ?? 0);
    });
  }, [filteredRows]);

  const columns = [
    { key: "Date", label: "Date", render: (r: Row) => pickDate(r) },
    { key: "ItemCode", label: "Item", render: (r: Row) => String(r.ItemCode ?? "-") },
    {
      key: "Predicted",
      label: "Recommended qty",
      render: (r: Row) => <PredictedValue row={r} value={toNum(r.prediction)} />,
    },
    {
      key: "Confidence",
      label: "Chance of selling",
      render: (r: Row) => <ConfidenceBadge value={toNum(r.p_demand)} />,
    },
    {
      key: "Range",
      label: "Likely range (low-high)",
      render: (r: Row) => {
        const lo = toNum(r.q_10);
        const hi = toNum(r.q_90);
        if (lo == null || hi == null) return "-";
        return `${lo.toFixed(1)} - ${hi.toFixed(1)}`;
      },
    },
  ];

  const windowLabel =
    summary.horizon > 0
      ? `${chartData[0].date} to ${chartData[chartData.length - 1].date}`
      : `from ${today}`;

  return (
    <Drawer open={open} onClose={onClose} title="Upcoming van load" width="xl">
      <div className="space-y-6">
        <DrawerContextBar
          routeCode={routeCode}
          itemCodes={itemCodes}
          dateRange={windowLabel}
          extra={
            filteredRows.length > 0 && (
              <span className="text-caption text-text-tertiary">
                {fmtNum(filteredRows.length)} lines
              </span>
            )
          }
        />

        {loading ? (
          <Loading message="Loading upcoming van load..." />
        ) : filteredRows.length === 0 ? (
          <EmptyState
            title="Nothing scheduled"
            message="No upcoming load from today onwards for this route/item selection."
            icon="📈"
          />
        ) : (
          <>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <MetricCard
                label="Days covered"
                value={`${summary.horizon} days`}
                subtitle={`${summary.skus} items`}
              />
              <MetricCard
                label="Total van load"
                value={fmtNum(summary.total, 1)}
                subtitle="Units across the window"
              />
              <MetricCard
                label="Avg per day"
                value={summary.horizon > 0 ? fmtNum(summary.total / summary.horizon, 1) : "-"}
                subtitle="Units / day"
              />
            </div>

            <LineChart
              title={`Daily van load${showBand ? "" : " - route total"}`}
              data={chartData}
              xKey="date"
              series={
                showBand
                  ? [
                      { key: "q90", label: "High estimate", color: CHART_COLOR.brandBand },
                      { key: "predicted", label: "Van load", color: CHART_COLOR.brandPrimary },
                      { key: "q10", label: "Low estimate", color: CHART_COLOR.brandBand },
                    ]
                  : [{ key: "predicted", label: "Van load" }]
              }
              height={320}
            />

            <Card
              title="Line-item detail"
              actions={<span className="text-body text-text-tertiary">{tableRows.length} rows</span>}
            >
              <Table data={tableRows} columns={columns} />
            </Card>
          </>
        )}
      </div>
    </Drawer>
  );
}
