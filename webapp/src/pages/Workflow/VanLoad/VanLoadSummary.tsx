import MetricCard from "@/components/charts/MetricCard";
import KpiRow from "@/components/ui/KpiRow";
import InfoBubble from "@/components/ui/InfoBubble";

import { fmtNum, fmtCurrency, fmtBps, AT_RISK_CONFIDENCE } from "@/lib/format";
import type { VanLoadSummaryView } from "@/api/forecast";

interface Props {
  /** Server-pre-computed; van_load_qty == carried_qty + issued_qty by construction. */
  summary: VanLoadSummaryView;
}

export default function VanLoadSummary({ summary }: Props) {
  const num = (value: number) => <span className="tabular-nums">{fmtNum(value)}</span>;

  return (
    <KpiRow>
      <MetricCard
        label="Recommended van load"
        value={
          <span className="inline-flex items-baseline gap-1.5">
            {num(summary.van_load_qty)}
            <span className="text-body font-normal text-text-tertiary">
              across {fmtNum(summary.van_load_items)} item
              {summary.van_load_items === 1 ? "" : "s"}
            </span>
          </span>
        }
        subtitle={
          <span>
            {"Carried from yesterday: "}
            {num(summary.carried_qty)}
            {` across ${fmtNum(summary.carried_items)} item${summary.carried_items === 1 ? "" : "s"} - Fresh from depot: `}
            {num(summary.issued_qty)}
            {` across ${fmtNum(summary.issued_items)} item${summary.issued_items === 1 ? "" : "s"}`}
          </span>
        }
        info={
          <InfoBubble
            title="What is the recommended van load?"
            body={
              <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                <p>
                  The total stock we recommend on the truck for this route + date: yesterday&apos;s
                  leftover already on the van plus the fresh stock the depot should issue today.
                </p>
                <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                  total van load = carried over + fresh from depot
                </p>
                <p>
                  Same number rendered on the Past-performance drawer&apos;s headline tile and used
                  as the input to the Recommendation-match accuracy metric there - one
                  recommendation, one definition.
                </p>
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
                <p>
                  The AED value of the recommended van load if every recommended unit sells at its
                  current average price.
                </p>
                <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                  expected_revenue = sum_i Predicted_i x AvgUnitPrice_i
                </p>
                <p>
                  <code>AvgUnitPrice</code> is the quantity-weighted mean over recent invoices for
                  that (route, item). Items without a recent price contribute nothing - the metric
                  reads <em>--</em> if no row in the load has a price.
                </p>
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
                <p>
                  Distinct <code>ItemCode</code>s that appear in the recommended van load - counted
                  whether they came from yesterday&apos;s leftover, today&apos;s fresh allocation,
                  or both.
                </p>
                <p>
                  An item only carried over (no fresh recommendation) still counts. An item with a
                  recommendation but no leftover also counts. Each <code>ItemCode</code> is counted
                  once regardless of which side contributed.
                </p>
              </div>
            }
          />
        }
      />
      <MetricCard
        label="Items unlikely to sell"
        value={fmtNum(summary.at_risk)}
        subtitle={`Less than ${fmtBps(AT_RISK_CONFIDENCE)} chance of selling today`}
        trend={summary.at_risk === 0 ? "up" : "down"}
        info={
          <InfoBubble
            title="What does 'unlikely to sell' mean?"
            body={
              <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                <p>
                  Items loaded on the truck today that our forecast estimates have less than a{" "}
                  {fmtBps(AT_RISK_CONFIDENCE)} chance of selling.
                </p>
                <p>
                  Use this as a quick watchlist — if the number is high, the supervisor may want to
                  review whether those items are worth taking on the route, or are likely to come
                  back at end of day.
                </p>
                <p>
                  Only items the model can score with a real probability are counted (consistently
                  slow or sporadic sellers). Steadily-selling items are excluded since their
                  probability is essentially &quot;always sells&quot;.
                </p>
              </div>
            }
          />
        }
      />
    </KpiRow>
  );
}
