import { useState } from "react";
import Card from "@/components/ui/Card";
import EmptyState from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import BarChart from "@/components/charts/BarChart";
import ToggleGroup, { type ToggleOption } from "@/components/ui/ToggleGroup";
import { CHART_COLOR } from "@/components/charts/theme";
import type { SalesOverviewResponse } from "@/types/data-import";

type TrendMetric = "revenue" | "quantity";

const TREND_METRIC_OPTIONS: ToggleOption<TrendMetric>[] = [
  { value: "revenue", label: "Revenue" },
  { value: "quantity", label: "Quantity" },
];

interface Props {
  data: SalesOverviewResponse | null;
  loading: boolean;
  height?: number;
}

/** Daily trend card with Revenue/Quantity toggle; one series at a time keeps the y-axis honest. */
export default function DashboardDailyTrend({ data, loading, height = 300 }: Props) {
  const [metric, setMetric] = useState<TrendMetric>("revenue");

  return (
    <Card
      title="Daily sales trend — actual customer purchases per day"
      actions={
        <ToggleGroup
          options={TREND_METRIC_OPTIONS}
          value={metric}
          onChange={setMetric}
          ariaLabel="Trend metric"
        />
      }
    >
      {loading ? (
        <Skeleton className="rounded-lg" style={{ height: `${height}px` }} />
      ) : !data?.daily_trend || data.daily_trend.length === 0 ? (
        <EmptyState title="No trend data" />
      ) : (
        <BarChart
          data={data.daily_trend as unknown as Record<string, unknown>[]}
          xKey="date"
          yKey={metric}
          color={metric === "revenue" ? CHART_COLOR.brandPrimary : CHART_COLOR.success}
          height={height}
        />
      )}
    </Card>
  );
}
