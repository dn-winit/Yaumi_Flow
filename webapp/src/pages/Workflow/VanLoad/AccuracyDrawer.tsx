import { useEffect, useMemo, useState } from "react";
import Drawer from "@/components/ui/Drawer";
import EmptyState from "@/components/ui/EmptyState";
import KpiRow from "@/components/ui/KpiRow";
import MetricCard from "@/components/charts/MetricCard";
import LineChart from "@/components/charts/LineChart";
import { Skeleton } from "@/components/ui/Skeleton";
import InfoBubble from "@/components/ui/InfoBubble";
import SectionLabel from "@/components/ui/SectionLabel";
import { CHART_COLOR } from "@/components/charts/theme";
import DashboardFilterBar from "@/pages/Dashboard/DashboardFilterBar";

import { useReconciliationPastPerformance } from "@/hooks/useForecast";
import {
  fmtNum,
  fmtCurrency,
  fmtPct,
  lookbackDays as lookbackDaysOf,
  type Lookback,
  DEFAULT_LOOKBACK,
} from "@/lib/format";
import { todayIso, addDays, fmtDate, fmtDateRange } from "@/lib/date";
import type { DashboardFilters } from "@/types/data-import";
import { EMPTY_FILTERS } from "@/types/data-import";

interface Props {
  open: boolean;
  onClose: () => void;
  routeCode?: string;
  /**
   * Last day of the past-performance window. When the page already
   * holds a user-selected date, pass it here so the drawer aligns with
   * the headline van-load tile. Falls back to "yesterday" only when
   * the caller has no date in scope.
   */
  endDate?: string;
}

/**
 * Past performance drawer. Reads canonical reconciled values from the
 * forecast frame the daily cron writes, so per-day totals match the
 * page-view tile by construction. Anchor scope: (route, item) pairs
 * with Predicted > 0 in the window. Wire shape and field semantics
 * are documented in src/types/forecast.ts.
 */
export default function AccuracyDrawer({ open, onClose, routeCode, endDate: endDateProp }: Props) {
  const [lookback, setLookback] = useState<Lookback>(DEFAULT_LOOKBACK);
  const [filters, setFilters] = useState<DashboardFilters>(EMPTY_FILTERS);

  // Reset/seed filter state on every open so the user always lands on
  // "this route's history" without lingering selections.
  useEffect(() => {
    if (!open) return;
    setFilters({
      ...EMPTY_FILTERS,
      route_codes: routeCode ? [routeCode] : [],
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, routeCode]);

  const lookbackDays = lookbackDaysOf(lookback);
  // Past performance is over COMPLETED days. If the caller hands us
  // today (page is showing the in-progress day), cap at yesterday so
  // the "Last working day" label is always truthful. ISO date strings
  // compare lexicographically.
  const yesterday = addDays(todayIso(), -1);
  const endDate =
    endDateProp && endDateProp < yesterday ? endDateProp : yesterday;

  const apiFilters = useMemo(
    () => ({
      item_codes: filters.item_codes,
      category_codes: filters.category_codes,
    }),
    [filters.item_codes, filters.category_codes],
  );

  const { data, loading } = useReconciliationPastPerformance(
    routeCode,
    open ? endDate : undefined,
    lookbackDays,
    open && Boolean(routeCode),
    apiFilters,
  );

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title="Past performance"
      width="xl"
    >
      <div className="space-y-6">
        <DashboardFilterBar
          value={filters}
          onChange={setFilters}
          lookback={lookback}
          onLookbackChange={setLookback}
          hideWarehouse
          hideRoute
        />
        {data?.active_days != null && (
          <div className="text-caption text-text-tertiary">
            {data.active_days} active day
            {data.active_days === 1 ? "" : "s"} in window
          </div>
        )}

        {loading ? (
          <DrawerSkeleton />
        ) : !data?.available || data.daily.length === 0 ? (
          <EmptyState
            title="No past activity for this window"
            message={data?.message ?? `No allocation or sales recorded for route ${routeCode ?? ""} in this period.`}
          />
        ) : (
          <DrawerContent data={data} />
        )}
      </div>
    </Drawer>
  );
}

function DrawerSkeleton() {
  return (
    <div className="space-y-6">
      <KpiRow>
        {[0, 1, 2].map((i) => (
          <Skeleton key={i} className="h-24" />
        ))}
      </KpiRow>
      <KpiRow>
        {[0, 1].map((i) => (
          <Skeleton key={i} className="h-24" />
        ))}
      </KpiRow>
      <Skeleton className="h-80" />
    </div>
  );
}

interface ContentProps {
  data: NonNullable<ReturnType<typeof useReconciliationPastPerformance>["data"]>;
}

// Pure render: server pre-computes total, percentages, ordering, and
// labels (single source of truth in the backend). UI only maps tone to
// a colour token. ``floor_protected`` is dropped server-side because
// the back-test path runs with ``use_carry_floor=False``.
function DrawerContent({ data }: ContentProps) {
  const t = data.totals;
  const m = data.metrics;

  // Surface the actual window the metrics cover: a single day for
  // "Last working day", a range for the multi-day lookbacks. Pulls
  // straight from the server response so the dates always match the
  // tiles below -- no client-side date math drift.
  const windowLabel = (() => {
    const start = data.start_date ?? "";
    const end = data.end_date ?? "";
    if (!start && !end) return null;
    if (!start || !end || start === end) return fmtDate(end || start);
    return fmtDateRange(start, end);
  })();

  return (
    <>
      {/* Plain-language explainer banner so a non-tech reader knows
          what they're looking at without needing to parse the tiles. */}
      <div className="rounded-lg border border-brand-100 bg-brand-50/50 p-4 text-body text-text-secondary">
        We compare the <strong>actual van load</strong> (what the rep took) against the{" "}
        <strong>recommended van load</strong> (what our forecast suggests) — with{" "}
        <strong>actually sold</strong> as the ground truth. Scoped to items we forecast on this route.
        Items whose dominant buyer isn't on the day's journey plan are intentionally zeroed out, so a
        flat recommendation on those days is by design, not a model miss.
        {windowLabel && (
          <>
            {" "}
            <span className="text-text-tertiary">Period: <strong>{windowLabel}</strong>.</span>
          </>
        )}
      </div>

      {/* Three story tiles -- plain English on the face, full math + source
          views one click away in the "i" bubble. */}
      <SectionLabel>Actual van load · recommended van load · actually sold</SectionLabel>
      <KpiRow>
        <MetricCard
          label="Actual van load"
          value={`${fmtNum(t.rep_van_load_total)} units`}
          subtitle={`${fmtNum(t.past_leftover_total)} kept from yesterday + ${fmtNum(t.today_allocation_total)} depot loaded`}
          info={
            <InfoBubble
              title="How is rep van load calculated?"
              body={
                <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                  <p><strong>Rep van load</strong> is what the rep physically had on the truck — the stock kept on board from yesterday plus what depot loaded fresh today.</p>
                  <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                    rep_van_load[d] = ClosingQty[d−1]  +  AllocatedPC[d]
                  </p>
                  <p>Both numbers come straight from SQL Server views, looked up directly per (route, item, day):</p>
                  <ul className="list-disc pl-5 space-y-1">
                    <li><strong>VW_GET_CLOSING_STOCK</strong> → yesterday's <code>ClosingQty</code> = leftover</li>
                    <li><strong>VW_GET_LOAD_ALLOCATION_DETAILS</strong> → today's <code>AllocatedPC</code> = fresh allocation</li>
                  </ul>
                  <p>If a row is missing for a given (item, day), we treat that value as 0. The schema never logs <code>ClosingQty=0</code> — empirically validated against 21,073 cells across 12 routes.</p>
                </div>
              }
            />
          }
          className="!border-l-warning-500"
        />
        <MetricCard
          label="Recommended van load"
          value={`${fmtNum(t.recommended_van_load_total)} units`}
          subtitle={
            t.recommended_carried_total != null && t.recommended_fresh_total != null
              ? `${fmtNum(t.recommended_carried_total)} kept from yesterday + ${fmtNum(t.recommended_fresh_total)} depot recommended (leftover minimised)`
              : "Yesterday's leftover plus what depot should issue today"
          }
          info={
            <InfoBubble
              title="How is our recommendation calculated?"
              body={
                <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                  <p><strong>Recommended van load</strong> reads the same reconciled cells the daily cron writes to the forecast table — so the number you see here matches the headline tile on the Van Load page byte-for-byte for the same (route, date).</p>
                  <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                    recommended_van_load[d] = opening_stock[d]  +  recommended_load[d]
                  </p>
                  <p>Per (item, day), the cron subtracts the simulated leftover from the bias-corrected forecast and rounds to whole units:</p>
                  <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                    P_corrected = Predicted / (1 + bias_pct)<br />
                    recommended_load = max(0, P_corrected − opening_stock)
                  </p>
                  <p><strong>Leftover minimisation:</strong> if the truck already has 100 of an item and demand is 80, the engine recommends zero fresh — less stock left on the van overnight.</p>
                  <p><strong>Journey-aware mask:</strong> for items where one or two customers account for nearly all sales, recommended_load is forced to 0 on dates those customers aren't on the day's journey plan. Loading 900 units of a wholesale-only item on a day the wholesaler isn't being visited is the kind of phantom load this guard prevents.</p>
                  <p><strong>bias_pct</strong> is the recency-weighted ratio of past actuals to past predictions over the last 30 days for that route+item, capped at ±50%.</p>
                </div>
              }
            />
          }
          className="!border-l-brand-600"
        />
        <MetricCard
          label="Actually sold"
          value={`${fmtNum(t.actual_sold_total)} units`}
          subtitle="What customers actually bought — the ground truth"
          info={
            <InfoBubble
              title="What 'actually sold' means"
              body={
                <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                  <p><strong>Actually sold</strong> is the invoiced sales total for the same items in the same window.</p>
                  <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                    Σ TotalQuantity from VW_GET_SALES_DETAILS<br />
                    WHERE TrxType = &apos;SalesInvoice&apos;<br />
                    AND ItemType = &apos;OrderItem&apos;
                  </p>
                  <p>Returns (bad/good) are tracked separately under <code>TrxType = &apos;Bad Return&apos;</code> / <code>&apos;Good Return&apos;</code> and are <strong>not</strong> counted as sales.</p>
                </div>
              }
            />
          }
          className="!border-l-success-600"
        />
      </KpiRow>

      {/* Three forecast-performance tiles -- each answers a UNIQUE
          question. No number on this row appears twice. Section symmetry
          with the row above (3 + 3) keeps the drawer scannable.
            • Accuracy  -- how close our forecast came to actual demand (%)
            • Coverage  -- how many items we predicted that the rep sold (%)
            • Saved     -- AED savings under our recommendation (with the
                           rep AED -> ours AED breakdown in the subtitle) */}
      <SectionLabel>Forecast performance</SectionLabel>
      <KpiRow>
        <MetricCard
          label="Recommendation match"
          value={fmtPct(m.forecast_accuracy_pct)}
          subtitle={`Recommended ${fmtNum(t.recommended_van_load_total)} vs actually sold ${fmtNum(t.actual_sold_total)}`}
          trend={m.forecast_accuracy_pct >= 80 ? "up" : "down"}
          info={
            <InfoBubble
              title="What is recommendation match?"
              body={
                <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                  <p>How close the <strong>recommended van load</strong> shown on the headline tile came to what was <strong>actually sold</strong> on this route. Bounded fill-ratio accuracy — symmetric, [0%, 100%], no cliff when over-allocation runs heavy.</p>
                  <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                    recommended_van_load  =  yesterday_leftover  +  fresh_recommended<br />
                    match  =  min(recommended_van_load, actually_sold)  ÷  max(recommended_van_load, actually_sold)  × 100
                  </p>
                  <p>For this view: recommended <strong>{fmtNum(t.recommended_van_load_total)}</strong> ({fmtNum(t.recommended_carried_total)} kept + {fmtNum(t.recommended_fresh_total)} fresh) vs actually sold <strong>{fmtNum(t.actual_sold_total)}</strong> = <strong>{fmtPct(m.forecast_accuracy_pct)}</strong>.</p>
                  <p>Symmetric: a 2× over-allocation and a 0.5× under-allocation both read as 50%. 100% = exact match. The number is naturally bounded by min/max, so heavy leftover days that would clamp a WAPE-style metric to 0% still show a meaningful signal here.</p>
                  <p className="text-text-tertiary"><em>Different from <strong>Baseline accuracy</strong> on the Pipeline page, which is the model&apos;s overall test-set score across all routes. This tile is route + window specific.</em></p>
                </div>
              }
            />
          }
          className="!border-l-brand-600"
        />
        <MetricCard
          label="Forecast coverage"
          value={fmtPct(m.forecast_coverage_pct, 0)}
          subtitle="Share of sold items that were on our forecast"
          trend={m.forecast_coverage_pct >= 80 ? "up" : "down"}
          info={
            <InfoBubble
              title="How is forecast coverage calculated?"
              body={
                <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                  <p>Of the items the rep actually sold on each working day in the window, what fraction were on our forecast for that day?</p>
                  <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                    coverage = mean over each working day of:<br />
                    &nbsp;&nbsp;|sold_items ∩ forecasted_items|  ÷  |sold_items|
                  </p>
                  <p>100% means every item the rep sold was something we&apos;d predicted demand for. Lower numbers mean some sales came from items the model never saw — those are blind spots to investigate.</p>
                </div>
              }
            />
          }
          className="!border-l-brand-600"
        />
        <MetricCard
          label={
            (t.excess_units_savings ?? 0) >= 0
              ? "Overnight stock prevented"
              : "Extra overnight stock"
          }
          // Signed value so the sign communicates direction ("+808" reads
          // as "808 more units left overnight than the rep" while "−808"
          // reads as "808 units saved"). The label and border colour
          // already reinforce the polarity; the signed number removes
          // any chance the reader misinterprets the magnitude.
          value={
            t.excess_units_savings != null
              ? `${t.excess_units_savings >= 0 ? "−" : "+"}${fmtNum(Math.abs(t.excess_units_savings))} units`
              : fmtCurrency(t.holding_savings)
          }
          subtitle={
            t.rep_excess_units != null && t.our_excess_units != null
              ? `Actual ${fmtNum(t.rep_excess_units)} → recommended ${fmtNum(t.our_excess_units)} units left on the truck overnight`
              : `Actual ${fmtCurrency(t.rep_holding_value)} → recommended ${fmtCurrency(t.our_holding_value)} (overnight excess stock)`
          }
          // Up = our policy reduced overnight stock (good).
          // Down = our policy left more overnight (bad).
          trend={
            (t.excess_units_savings ?? t.holding_savings) > 0
              ? "up"
              : (t.excess_units_savings ?? t.holding_savings) < 0
              ? "down"
              : undefined
          }
          info={
            <InfoBubble
              title="How is overnight stock calculated?"
              body={
                <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                  <p><strong>Overnight stock</strong> is the units loaded onto the truck that didn&apos;t sell that day — pieces that have to be stored, recounted, and rolled into tomorrow&apos;s leftover. Less overnight stock = leaner van, less stale inventory, lower handling overhead.</p>
                  <p>Per item we compute:</p>
                  <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                    overnight_units  =  max( on_truck − sold,  0 )<br />
                    total_overnight  =  Σ overnight_units
                  </p>
                  <p>The <em>max(., 0)</em> excludes items where stock ran short — those would be lost-sales cost, not overnight stock.</p>
                  <p>Numbers shown:</p>
                  <ul className="list-disc pl-5 space-y-1">
                    <li>Under the actual van load: <strong>{fmtNum(t.rep_excess_units ?? 0)} units</strong> ({fmtCurrency(t.rep_holding_value)})</li>
                    <li>Under the recommended van load: <strong>{fmtNum(t.our_excess_units ?? 0)} units</strong> ({fmtCurrency(t.our_holding_value)})</li>
                    <li>Units prevented: <strong>{fmtNum(t.excess_units_savings ?? 0)}</strong> · AED equivalent: <strong>{fmtCurrency(t.holding_savings)}</strong></li>
                  </ul>
                  <p>Why two figures? The bias-corrected forecast can redistribute issuance toward higher-priced items the model has been under-predicting; that can lift AED while units fall. <strong>Units</strong> is the metric the policy directly controls and the cleaner leftover-minimisation signal; <strong>AED</strong> is the financial context.</p>
                </div>
              }
            />
          }
          className={
            (t.excess_units_savings ?? t.holding_savings) >= 0
              ? "!border-l-success-600"
              : "!border-l-warning-500"
          }
        />
      </KpiRow>

      {/* Daily comparison chart -- one line per story. The recommended-
          van-load series plots ``recommended_van_load`` (leftover + fresh
          composition), matching the headline tile exactly. The
          forecast-quality story lives on its own ``Forecast accuracy``
          tile -- not on this chart -- so each surface shows ONE meaning
          of "recommended" and the two never collide. */}
      <LineChart
        title="Day-by-day comparison"
        subtitle="Actual van load (yellow) · recommended van load (blue) · actually sold (green, the ground truth)"
        data={data.daily as unknown as Record<string, unknown>[]}
        xKey="date"
        series={[
          {
            key: "rep_van_load",
            label: "Actual van load",
            color: CHART_COLOR.warning,
          },
          {
            key: "recommended_van_load",
            label: "Recommended van load",
            color: CHART_COLOR.brandPrimary,
          },
          {
            key: "actual_sold",
            label: "Actually sold",
            color: CHART_COLOR.success,
          },
        ]}
        height={320}
      />
    </>
  );
}
