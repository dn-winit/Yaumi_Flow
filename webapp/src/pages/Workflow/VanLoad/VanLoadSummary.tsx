import MetricCard from "@/components/charts/MetricCard";
import KpiRow from "@/components/ui/KpiRow";
import InfoBubble from "@/components/ui/InfoBubble";

import { fmtNum, fmtCurrency, fmtBps, AT_RISK_CONFIDENCE } from "@/lib/format";
import type { VanLoadSummaryView } from "@/api/forecast";

interface Props {
  /** Pre-computed summary from the backend page-view endpoint. The
   *  server has already enforced van_load_qty == carried_qty +
   *  issued_qty, so the tile and the chart on this page cannot disagree. */
  summary: VanLoadSummaryView;
}

// Format "X across Y items" caption from a server-supplied quantity +
// item count so every quantity in the tile reads the same way.
function fmtAcross(qty: number, items: number): string {
  return `${fmtNum(qty)} across ${fmtNum(items)} item${items === 1 ? "" : "s"}`;
}

export default function VanLoadSummary({ summary }: Props) {
  return (
    <KpiRow>
      <MetricCard
        label="Recommended van load"
        value={fmtAcross(summary.van_load_qty, summary.van_load_items)}
        subtitle={
          `Carried from yesterday: ${fmtAcross(summary.carried_qty, summary.carried_items)}` +
          ` - Fresh from depot: ${fmtAcross(summary.issued_qty, summary.issued_items)}`
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
