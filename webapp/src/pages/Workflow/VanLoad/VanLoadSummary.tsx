import MetricCard from "@/components/charts/MetricCard";
import KpiRow from "@/components/ui/KpiRow";
import InfoBubble from "@/components/ui/InfoBubble";
import BreakdownPopover, {
  type BreakdownValueField,
} from "@/components/ui/BreakdownPopover";

import { fmtNum, fmtCurrency, fmtBps, AT_RISK_CONFIDENCE } from "@/lib/format";
import type { VanLoadSummaryView } from "@/api/forecast";
import type { PastPerformanceItem } from "@/types/forecast";

interface Props {
  /** Pre-computed summary from the backend page-view endpoint. The
   *  server has already enforced van_load_qty == carried_qty +
   *  issued_qty, so the tile and the chart on this page cannot disagree. */
  summary: VanLoadSummaryView;
  /** Per-(item, date) rows for the click-to-explain popovers on this
   *  tile. Scoped to a single date (the page's date) -- one row per
   *  item. Comes straight off the page-view wire; the tile does not
   *  filter or sort. */
  items: PastPerformanceItem[];
  /** ISO date string the tile is scoped to. Rendered in each popover
   *  sub-header so the user always sees which day the rows are for. */
  date: string;
}

export default function VanLoadSummary({ summary, items, date }: Props) {
  // Single-line sub-header used on every popover for this tile. Date
  // comes straight off the page-view wire; the tile does not format
  // beyond prefixing "Window:" for parity with the past-performance
  // surface (single-day window).
  const popoverWindow = `Window: ${date}`;

  // Helper to wrap a single quantity in a popover trigger. The "across
  // Y items" caption stays plain text -- only the quantity is clickable.
  const numClick = (
    value: number,
    title: string,
    field: BreakdownValueField,
  ) => (
    <BreakdownPopover
      trigger={<span className="tabular-nums">{fmtNum(value)}</span>}
      title={title}
      windowLabel={popoverWindow}
      items={items}
      valueField={field}
      totalValue={value}
    />
  );

  return (
    <KpiRow>
      <MetricCard
        label="Recommended van load"
        value={
          <span className="inline-flex items-baseline gap-1.5">
            {numClick(
              summary.van_load_qty,
              "Recommended van load breakdown",
              "recommended_van_load",
            )}
            <span className="text-body font-normal text-text-tertiary">
              across {fmtNum(summary.van_load_items)} item
              {summary.van_load_items === 1 ? "" : "s"}
            </span>
          </span>
        }
        subtitle={
          <span>
            {"Carried from yesterday: "}
            {numClick(
              summary.carried_qty,
              "Carried from yesterday breakdown",
              "recommended_carried",
            )}
            {` across ${fmtNum(summary.carried_items)} item${summary.carried_items === 1 ? "" : "s"} - Fresh from depot: `}
            {numClick(
              summary.issued_qty,
              "Fresh from depot breakdown",
              "recommended_fresh",
            )}
            {` across ${fmtNum(summary.issued_items)} item${summary.issued_items === 1 ? "" : "s"}`}
          </span>
        }
        info={
          <InfoBubble
            title="What is the recommended van load?"
            body={
              <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                <p>The total stock the engine recommends be on the truck for this route+date: yesterday&apos;s leftover already on the van plus the fresh allocation depot should issue today.</p>
                <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                  recommended_van_load  =  carried_from_yesterday  +  fresh_from_depot<br />
                  fresh_from_depot       =  max(0, P_corrected - leftover)<br />
                  P_corrected            =  Predicted / (1 + bias_pct)
                </p>
                <p>Same number rendered on the Past-performance drawer&apos;s headline tile and used as the input to the Recommendation-match accuracy metric there - one recommendation, one definition.</p>
              </div>
            }
          />
        }
      />
      <MetricCard
        label="Expected revenue"
        value={summary.has_revenue ? fmtCurrency(summary.revenue) : "--"}
        subtitle={summary.has_revenue ? "Units x unit price" : "No price data"}
        info={
          <InfoBubble
            title="How is expected revenue calculated?"
            body={
              <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                <p>The AED value of the recommended van load if every recommended unit sells at its current average price.</p>
                <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                  expected_revenue  =  sum_i  Predicted_i x AvgUnitPrice_i
                </p>
                <p><code>AvgUnitPrice</code> is the quantity-weighted mean over recent invoices for that (route, item). Items without a recent price contribute nothing - the metric reads <em>--</em> if no row in the load has a price.</p>
              </div>
            }
          />
        }
      />
      <MetricCard
        label="Different items"
        value={fmtNum(summary.van_load_items)}
        subtitle="Distinct items on the van (carried + fresh)"
        info={
          <InfoBubble
            title="What counts as a different item?"
            body={
              <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                <p>Distinct <code>ItemCode</code>s that appear in the recommended van load - counted whether they came from yesterday&apos;s leftover, today&apos;s fresh allocation, or both.</p>
                <p>An item only carried over (no fresh recommendation) still counts. An item with a recommendation but no leftover also counts. Each <code>ItemCode</code> is counted once regardless of which side contributed.</p>
              </div>
            }
          />
        }
      />
      <MetricCard
        label="Risky items"
        value={fmtNum(summary.at_risk)}
        subtitle={`Below ${fmtBps(AT_RISK_CONFIDENCE)} chance of selling`}
        trend={summary.at_risk === 0 ? "up" : "down"}
        info={
          <InfoBubble
            title="What makes an item risky?"
            body={
              <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                <p>An item on the load whose model probability of selling on this date falls below {fmtBps(AT_RISK_CONFIDENCE)}.</p>
                <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                  risky_items  =  count{`{ p_demand < ${AT_RISK_CONFIDENCE} }`}
                </p>
                <p><code>p_demand</code> is a real probability only for two-stage classes (intermittent, lumpy). Smooth and erratic items emit a synthetic 0/1 signal - those are excluded from the risky count so the metric isn&apos;t skewed by binary fallbacks.</p>
                <p>Use the count as a watchlist: a high number means the supervisor should review whether those items are worth loading.</p>
              </div>
            }
          />
        }
      />
    </KpiRow>
  );
}
