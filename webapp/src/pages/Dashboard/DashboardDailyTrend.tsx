import { useState } from "react";
import Card from "@/components/ui/Card";
import EmptyState from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import LineChart from "@/components/charts/LineChart";
import type { SalesOverviewResponse } from "@/types/data-import";

type TrendMetric = "revenue" | "quantity";

const TREND_METRIC_OPTIONS: { key: TrendMetric; label: string }[] = [
  { key: "revenue", label: "Revenue" },
  { key: "quantity", label: "Quantity" },
];

interface Props {
  data: SalesOverviewResponse | null;
  loading: boolean;
  height?: number;
}

/**
 * Shared full-width daily trend card with a Revenue / Quantity toggle.
 * Showing one series at a time keeps the y-axis honest -- mixing AED and
 * units on a shared axis distorts whichever one is smaller.
 *
 * Toggle state is component-local so each instance (Dashboard page,
 * VanLoad "Past analysis" drawer, etc.) tracks its own active metric.
 */
export default function DashboardDailyTrend({ data, loading, height = 300 }: Props) {
  const [metric, setMetric] = useState<TrendMetric>("revenue");

  return (
    <Card
      title="Daily sales trend — actual customer purchases per day"
      actions={<TrendMetricToggle active={metric} onChange={setMetric} />}
    >
      {loading ? (
        <Skeleton className="rounded-lg" style={{ height: `${height}px` }} />
      ) : !data?.daily_trend || data.daily_trend.length === 0 ? (
        <EmptyState title="No trend data" />
      ) : (
        <LineChart
          data={data.daily_trend as unknown as Record<string, unknown>[]}
          xKey="date"
          series={[
            metric === "revenue"
              ? { key: "revenue", label: "Revenue" }
              : { key: "quantity", label: "Quantity" },
          ]}
          height={height}
        />
      )}
    </Card>
  );
}

function TrendMetricToggle({
  active,
  onChange,
}: {
  active: TrendMetric;
  onChange: (m: TrendMetric) => void;
}) {
  return (
    <div className="inline-flex items-center rounded-md border border-default bg-surface-base p-0.5 gap-0.5">
      {TREND_METRIC_OPTIONS.map((opt) => {
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
