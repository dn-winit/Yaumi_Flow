import KpiRow from "@/components/ui/KpiRow";
import MetricCard from "@/components/charts/MetricCard";
import { fmtNum, fmtCurrency } from "@/lib/format";
import type { BusinessKpis } from "@/types/data-import";

const PLACEHOLDER = "—";

/**
 * Four headline tiles for the executive view. Pure function of a
 * BusinessKpis payload -- shared between the Dashboard page and the
 * VanLoad "Past analysis" drawer so both speak the same numbers.
 *
 * Math (every aggregate / average has its denominator visible in the
 * subtitle so a reader can verify with mental arithmetic):
 *   1. Total revenue       -- AED invoiced over the working-day window
 *   2. Total volume        -- units sold + transactions
 *   3. Unique items sold   -- distinct SKUs + per-route forecast coverage
 *   4. Lost opportunity    -- AED forecasted that didn't sell
 */
export default function DashboardKpis({ k }: { k: BusinessKpis | null }) {
  const revenue = k?.total_revenue;
  const volume = k?.total_volume;
  const items = k?.unique_items;
  const lost = k?.lost_opportunity;
  const workingDays = k?.working_days ?? 0;
  const coveredDays = k?.covered_days ?? 0;

  return (
    <KpiRow>
      <MetricCard
        label="Total revenue"
        value={revenue?.amount != null ? fmtCurrency(revenue.amount) : PLACEHOLDER}
        subtitle={
          revenue?.daily_avg != null && workingDays > 0
            ? `≈ ${fmtCurrency(revenue.daily_avg)}/day · ${workingDays} working days`
            : "invoiced sales"
        }
      />
      <MetricCard
        label="Total volume"
        value={volume?.units != null ? `${fmtNum(volume.units)} units` : PLACEHOLDER}
        subtitle={
          volume?.daily_avg_units != null && volume?.transactions != null
            ? `≈ ${fmtNum(volume.daily_avg_units)}/day · ${fmtNum(volume.transactions)} transactions`
            : volume?.transactions != null
            ? `across ${fmtNum(volume.transactions)} transactions`
            : ""
        }
      />
      <MetricCard
        label="Unique items sold"
        value={items?.count != null ? fmtNum(items.count) : PLACEHOLDER}
        subtitle={
          items?.daily_avg != null && items?.avg_daily_coverage_pct != null
            ? `≈ ${fmtNum(items.daily_avg)}/day · ${items.avg_daily_coverage_pct.toFixed(0)}% per-route forecast coverage`
            : items?.daily_avg != null
            ? `≈ ${fmtNum(items.daily_avg)} unique items per day`
            : "no forecast scored for this period"
        }
      />
      <MetricCard
        label="Lost opportunity"
        value={lost?.amount != null ? fmtCurrency(lost.amount) : PLACEHOLDER}
        subtitle={
          lost?.daily_avg != null && coveredDays > 0
            ? `≈ ${fmtCurrency(lost.daily_avg)}/day · ${coveredDays} forecast days`
            : lost?.items_affected != null && lost.items_affected > 0
            ? `${fmtNum(lost.units)} units across ${fmtNum(lost.items_affected)} items left unsold`
            : "every forecast unit sold through"
        }
      />
    </KpiRow>
  );
}
