import { useMemo } from "react";
import MetricCard from "@/components/charts/MetricCard";

import { fmtNum, fmtCurrency, toNum, AT_RISK_CONFIDENCE } from "@/lib/format";

interface Props {
  rows: Record<string, unknown>[];
  /** Latest date in the forecast horizon -- shown as a freshness hint, not a KPI. */
  lastForecastDate?: string | null;
}

export default function VanLoadSummary({ rows, lastForecastDate }: Props) {
  const metrics = useMemo(() => {
    const unique = new Set<string>();
    let totalQty = 0;
    let revenue = 0;
    let hasRevenue = false;
    let atRisk = 0;

    rows.forEach((r) => {
      const item = r.ItemCode;
      if (typeof item === "string" && item) unique.add(item);

      const predicted = toNum(r.prediction) ?? 0;
      totalQty += predicted;

      const price =
        toNum(r.AvgUnitPrice) ?? toNum(r.avg_unit_price) ?? toNum(r.unit_price);
      if (price != null) {
        hasRevenue = true;
        revenue += predicted * price;
      }

      const p = toNum(r.p_demand);
      if (p != null && p < AT_RISK_CONFIDENCE) atRisk += 1;
    });

    return {
      skus: unique.size,
      totalQty,
      revenue: hasRevenue ? revenue : null,
      hasRevenue,
      atRisk,
    };
  }, [rows]);

  return (
    <div className="space-y-2">
      {lastForecastDate && (
        <p className="text-caption text-text-tertiary">
          Recommendations available through <span className="font-medium text-text-secondary">{lastForecastDate}</span>
        </p>
      )}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <MetricCard
          label="Van load"
          value={fmtNum(metrics.totalQty, 0)}
          subtitle="Units to carry today"
        />
        <MetricCard
          label="Expected revenue"
          value={metrics.hasRevenue ? fmtCurrency(metrics.revenue) : "--"}
          subtitle={metrics.hasRevenue ? "Load x unit price" : "No price data"}
        />
        <MetricCard
          label="Items on van"
          value={fmtNum(metrics.skus)}
          subtitle="Distinct products"
        />
        <MetricCard
          label="Uncertain items"
          value={fmtNum(metrics.atRisk)}
          subtitle={`Less than ${Math.round(AT_RISK_CONFIDENCE * 100)}% chance of selling`}
          trend={metrics.atRisk === 0 ? "up" : "down"}
        />
      </div>
    </div>
  );
}
