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
 * Copy aim: a non-technical reader can understand each tile in one
 * glance. No jargon ("transactions" -> "invoices", "working days" ->
 * "active sales days", etc.). Every average shows its denominator
 * inline so the math is verifiable on the surface.
 *
 * Underlying maths (kept close to the UI for traceability):
 *   1. Total revenue       -- AED invoiced over the period
 *   2. Total volume        -- units sold + invoice count
 *   3. Unique items sold   -- distinct SKUs sold + how well we
 *                             predicted them per route
 *   4. Stock we forecast that didn't sell
 *                          -- AED of over-forecast
 *                             (Σ max(predicted − actual, 0) × price)
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
            ? `${fmtCurrency(revenue.daily_avg)} a day, on average · over the last ${workingDays} sales days`
            : "Invoiced over the period"
        }
      />
      <MetricCard
        label="Total volume"
        value={volume?.units != null ? `${fmtNum(volume.units)} units` : PLACEHOLDER}
        subtitle={
          volume?.daily_avg_units != null && volume?.transactions != null
            ? `${fmtNum(volume.daily_avg_units)} units a day · across ${fmtNum(volume.transactions)} invoices`
            : volume?.transactions != null
            ? `Across ${fmtNum(volume.transactions)} invoices`
            : ""
        }
      />
      <MetricCard
        label="Unique items sold"
        value={items?.count != null ? `${fmtNum(items.count)} items` : PLACEHOLDER}
        subtitle={
          items?.daily_avg != null && items?.avg_daily_coverage_pct != null
            ? `${fmtNum(items.daily_avg)} different items a day · forecast covered ${items.avg_daily_coverage_pct.toFixed(0)}% of what each route actually sold`
            : items?.daily_avg != null
            ? `${fmtNum(items.daily_avg)} different items on a typical day`
            : "No forecast scored for this period"
        }
      />
      <MetricCard
        label="Forecast we shipped that didn't sell"
        value={lost?.amount != null ? fmtCurrency(lost.amount) : PLACEHOLDER}
        subtitle={
          lost?.daily_avg != null && coveredDays > 0
            ? `${fmtCurrency(lost.daily_avg)} of stock left over a day · ${fmtNum(lost.items_affected)} items affected over ${coveredDays} days`
            : lost?.items_affected != null && lost.items_affected > 0
            ? `${fmtNum(lost.units)} units across ${fmtNum(lost.items_affected)} items left unsold`
            : "Every unit we forecast actually sold"
        }
      />
    </KpiRow>
  );
}
