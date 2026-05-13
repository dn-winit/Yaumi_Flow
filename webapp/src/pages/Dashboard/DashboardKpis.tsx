import KpiRow from "@/components/ui/KpiRow";
import MetricCard from "@/components/charts/MetricCard";
import InfoBubble from "@/components/ui/InfoBubble";
import SectionLabel from "@/components/ui/SectionLabel";
import { fmtNum, fmtCurrency } from "@/lib/format";
import type { BusinessKpis } from "@/types/data-import";

const PLACEHOLDER = "—";

/**
 * Dashboard KPI tiles -- pure SALES REALITY for the executive view.
 * Three numbers describing what's actually moving through the business:
 *
 *    • Total revenue        -- AED invoiced over the period
 *    • Total volume         -- units sold + invoice count
 *    • Unique items sold    -- distinct SKUs sold
 *
 * Forecast-performance metrics (accuracy, coverage, sales-opportunity)
 * live in the Past Performance drawer on the Plan page, where the
 * forecast vs actual comparison happens with route + window context.
 * Keeping Dashboard purely sales-reality avoids cross-surface
 * duplication and makes each page's purpose obvious at a glance.
 *
 * Every tile carries an "i" bubble so a reader who wants the formula
 * and source view can see it without cluttering the tile face.
 */
export default function DashboardKpis({ k }: { k: BusinessKpis | null }) {
  const revenue = k?.total_revenue;
  const volume = k?.total_volume;
  const items = k?.unique_items;
  const workingDays = k?.working_days ?? 0;

  return (
    <div className="space-y-3">
      <SectionLabel>What&apos;s actually selling</SectionLabel>
      <KpiRow>
        <MetricCard
          label="Total revenue"
          value={revenue?.amount != null ? fmtCurrency(revenue.amount) : PLACEHOLDER}
          subtitle={
            revenue?.daily_avg != null && workingDays > 0
              ? `${fmtCurrency(revenue.daily_avg)} a day, on average · over ${workingDays} sales days`
              : "Invoiced over the period"
          }
          info={
            <InfoBubble
              title="Total revenue"
              body={
                <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                  <p>AED of customer invoices over the selected period.</p>
                  <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                    Σ TotalQuantity × UnitPrice<br />
                    FROM VW_GET_SALES_DETAILS<br />
                    WHERE TrxType = &apos;SalesInvoice&apos;<br />
                    AND ItemType = &apos;OrderItem&apos;
                  </p>
                  <p>The daily average divides this total by the number of <em>active sales days</em> in the window (days where any invoice was booked) — not calendar days.</p>
                </div>
              }
            />
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
          info={
            <InfoBubble
              title="Total volume"
              body={
                <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                  <p>Total units sold and number of invoices booked over the period.</p>
                  <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                    units = Σ TotalQuantity (TrxType = &apos;SalesInvoice&apos;)<br />
                    invoices = COUNT(rows in window)
                  </p>
                </div>
              }
            />
          }
        />
        <MetricCard
          label="Unique items sold"
          value={items?.count != null ? `${fmtNum(items.count)} items` : PLACEHOLDER}
          subtitle={
            items?.daily_avg != null
              ? `${fmtNum(items.daily_avg)} different items on a typical day`
              : "No sales recorded"
          }
          info={
            <InfoBubble
              title="Unique items sold"
              body={
                <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                  <p>Distinct items that appeared in any invoice over the period.</p>
                  <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                    count = nunique(ItemCode) in VW_GET_SALES_DETAILS<br />
                    daily_avg = mean of nunique(ItemCode) per active sales day
                  </p>
                </div>
              }
            />
          }
        />
      </KpiRow>
    </div>
  );
}
