import { useMemo } from "react";
import MetricCard from "@/components/charts/MetricCard";

import { fmtNum, fmtCurrency, toNum, AT_RISK_CONFIDENCE } from "@/lib/format";

interface Props {
  rows: Record<string, unknown>[];
}

export default function VanLoadSummary({ rows }: Props) {
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
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
      <MetricCard
        label="Units to load"
        value={fmtNum(metrics.totalQty)}
        subtitle="Pieces to carry today"
      />
      <MetricCard
        label="Expected revenue"
        value={metrics.hasRevenue ? fmtCurrency(metrics.revenue) : "--"}
        subtitle={metrics.hasRevenue ? "Units x unit price" : "No price data"}
      />
      <MetricCard
        label="Different items"
        value={fmtNum(metrics.skus)}
        subtitle="Distinct products on the van"
      />
      <MetricCard
        label="Risky items"
        value={fmtNum(metrics.atRisk)}
        subtitle={`Below ${Math.round(AT_RISK_CONFIDENCE * 100)}% chance of selling`}
        trend={metrics.atRisk === 0 ? "up" : "down"}
      />
    </div>
  );
}
