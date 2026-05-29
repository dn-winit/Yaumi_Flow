import { useEffect, useMemo, useState } from "react";
import Drawer from "@/components/ui/Drawer";
import Loading from "@/components/ui/Loading";
import EmptyState from "@/components/ui/EmptyState";
import KpiRow from "@/components/ui/KpiRow";
import HighlightsStrip, { type Highlight } from "@/components/ui/HighlightsStrip";
import MetricCard from "@/components/charts/MetricCard";
import LineChart from "@/components/charts/LineChart";
import BarChart from "@/components/charts/BarChart";
import InfoBubble from "@/components/ui/InfoBubble";
import SectionLabel from "@/components/ui/SectionLabel";
import { CHART_COLOR } from "@/components/charts/theme";
import DashboardFilterBar from "@/pages/Dashboard/DashboardFilterBar";

import { useAdoption } from "@/hooks/useRecommendedOrder";
import { useLastActiveDate } from "@/hooks/useDataImport";
import { addDays, fmtDate, fmtDateRange, todayIso } from "@/lib/date";
import { fmtNum, fmtCurrency, fmtPct, fmtBps, DELIVERY_GOOD } from "@/lib/format";
import type { DashboardFilters, ReportingPeriod } from "@/types/data-import";
import { EMPTY_FILTERS } from "@/types/data-import";

const MISSING = "--";

interface Props {
  open: boolean;
  onClose: () => void;
  // Route is fixed (drawer opens from a live session); warehouse + route are hidden.
  routeCode?: string;
}

/** Past-analysis drawer for recommendation adoption; mirrors the Plan-step drawer.
 *  /analytics/adoption honours category + item filters, so the views are real scoped data. */
export default function AdoptionDrawer({ open, onClose, routeCode }: Props) {
  // Anchor on actual last active date so a Monday open onto Saturday doesn't show empty.
  const { date: lastActiveDate, loading: lastActiveLoading } = useLastActiveDate();

  const [period, setPeriod] = useState<ReportingPeriod | null>(null);
  const [filters, setFilters] = useState<DashboardFilters>(EMPTY_FILTERS);

  // Seed filters + period on open/route-change/last-active-shift. Falls back to today
  // if CSV is empty so the drawer always renders (instead of infinite loading state).
  useEffect(() => {
    if (!open) return;
    if (lastActiveLoading) return;
    setFilters({
      ...EMPTY_FILTERS,
      route_codes: routeCode ? [routeCode] : [],
    });
    const anchor = lastActiveDate ?? todayIso();
    setPeriod({
      start_date: addDays(anchor, -29),
      end_date: anchor,
    });
  }, [open, routeCode, lastActiveDate, lastActiveLoading]);

  // Picker emits ISO (start_date, end_date) directly; empty windows surface as empty-state below.
  const params = useMemo(
    () =>
      period
        ? {
            start_date: period.start_date,
            end_date: period.end_date,
            ...(filters.route_codes.length === 1 ? { route_code: filters.route_codes[0] } : {}),
            ...(filters.category_codes.length > 0
              ? { category_codes: filters.category_codes }
              : {}),
            ...(filters.item_codes.length > 0 ? { item_codes: filters.item_codes } : {}),
          }
        : null,
    [period, filters],
  );

  const { data, loading } = useAdoption(
    params ?? { start_date: "", end_date: "" },
    open && params != null,
  );
  const s = data?.summary ?? null;

  // Server-aligned daily series (adoption_pct=null on no-rec days → chart shows a break).
  const windowLabel =
    data?.start_date && data?.end_date ? fmtDateRange(data.start_date, data.end_date) : MISSING;
  const dailyPadded = data?.daily ?? [];
  const daysWithRecs = s?.days_with_recs ?? 0;
  const activeDays = s?.active_days ?? 0;

  const highlights = useMemo<Highlight[]>(() => {
    if (!s) return [];
    const items: Highlight[] = [];
    if (s.best_day) {
      items.push({
        label: "Best day",
        value: `${fmtPct(s.best_day.pct)} hit rate`,
        detail: fmtDate(s.best_day.date),
      });
    }
    if (s.skus_adopted > 0) {
      items.push({
        label: "Items captured",
        value: `${fmtNum(s.skus_adopted)} items`,
        detail: "Recommended and bought in the window",
      });
    }
    return items;
  }, [s]);

  const revenueArrow: "up" | "down" | undefined =
    s?.driven_revenue != null && s.driven_revenue > 0 ? "up" : undefined;
  const accuracyArrow: "up" | "down" | undefined =
    s?.pick_accuracy_pct == null ? undefined : s.pick_accuracy_pct >= DELIVERY_GOOD ? "up" : "down";
  const perfectPickArrow: "up" | "down" | undefined =
    s?.perfect_pick_pct == null ? undefined : s.perfect_pick_pct >= DELIVERY_GOOD ? "up" : "down";

  const singleDay = period != null && period.start_date === period.end_date;

  return (
    <Drawer
      open={open}
      onClose={onClose}
      title="Past performance - what customers actually bought"
      width="xl"
    >
      <div className="space-y-6">
        {period == null ? (
          <Loading message="Finding the latest day with activity..." />
        ) : (
          <>
            <DashboardFilterBar
              value={filters}
              onChange={setFilters}
              period={period}
              onPeriodChange={setPeriod}
              maxDate={lastActiveDate ?? undefined}
              hideWarehouse
              hideRoute
            />

            {loading || lastActiveLoading ? (
              <Loading message="Checking which recommendations sold..." />
            ) : !data?.available ? (
              <EmptyState
                icon="📭"
                title={
                  singleDay
                    ? `No recommendations recorded on ${fmtDate(period.end_date)}`
                    : "Not enough history"
                }
                message={
                  data?.message ??
                  (singleDay
                    ? `This date may be a weekend, holiday, or a day with no route activity. Try the previous working day or widen the period.`
                    : `No recommendations were stored for ${windowLabel} matching the current filters.`)
                }
              />
            ) : (
              <>
                <SectionLabel>How customers responded to our recommendations</SectionLabel>
                <KpiRow>
                  <MetricCard
                    label="Revenue from our suggestions"
                    value={
                      s?.driven_revenue != null && s.driven_revenue > 0
                        ? fmtCurrency(s.driven_revenue)
                        : s?.driven_volume
                          ? `${fmtNum(s.driven_volume)} units`
                          : "-"
                    }
                    subtitle={
                      !s || s.recommended_volume <= 0
                        ? "No recommendations bought yet"
                        : `${fmtNum(s.driven_volume)} of ${fmtNum(s.recommended_volume)} suggested units actually sold - ${fmtNum(s.skus_adopted)} items`
                    }
                    trend={revenueArrow}
                    info={
                      <InfoBubble
                        title="Revenue from our suggestions"
                        body={
                          <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                            <p>
                              AED of customer purchases that came from items we recommended to that
                              customer.
                            </p>
                            <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                              driven_revenue = Σ (qty_bought × unit_price)
                              <br />
                              where (customer, item, day) appears in BOTH:
                              <br />
                              &nbsp;&nbsp;recommended_orders.csv AND
                              <br />
                              &nbsp;&nbsp;VW_GET_SALES_DETAILS (TrxType=&apos;SalesInvoice&apos;)
                            </p>
                            <p>
                              This isolates the revenue our recommendations <em>actually</em> drove
                              - not just total revenue, but the slice attributable to suggestions
                              the rep made.
                            </p>
                          </div>
                        }
                      />
                    }
                    className="!border-l-success-600"
                  />
                  <MetricCard
                    label="Items the customer took"
                    value={s?.pick_accuracy_pct != null ? fmtPct(s.pick_accuracy_pct) : "-"}
                    subtitle={
                      s && s.skus_recommended > 0
                        ? `${fmtNum(s.skus_adopted)} of ${fmtNum(s.skus_recommended)} suggested items were bought`
                        : "No recommendations to score"
                    }
                    trend={accuracyArrow}
                    info={
                      <InfoBubble
                        title="Items the customer took"
                        body={
                          <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                            <p>
                              The percentage of items we recommended that the customer actually
                              bought.
                            </p>
                            <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                              pick_accuracy = items_adopted ÷ items_recommended × 100
                            </p>
                            <p>
                              Counts each (customer, item, day) once. A high number means the
                              rep&apos;s recommendation list was on-target with what the customer
                              wanted.
                            </p>
                          </div>
                        }
                      />
                    }
                    className="!border-l-brand-600"
                  />
                  <MetricCard
                    label="Right quantity, right item"
                    value={s?.perfect_pick_pct != null ? fmtPct(s.perfect_pick_pct) : "-"}
                    subtitle={
                      s && s.skus_adopted > 0
                        ? `${fmtNum(s.skus_perfect)} of ${fmtNum(s.skus_adopted)} bought within +/-${fmtBps(s.perfect_pick_tolerance)} of suggested`
                        : "No adopted items to score"
                    }
                    trend={perfectPickArrow}
                    info={
                      <InfoBubble
                        title="Right quantity, right item"
                        body={
                          <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                            <p>
                              Of the items the customer bought from our recommendations, the
                              percentage where the bought quantity was within +/-
                              {s ? fmtBps(s.perfect_pick_tolerance) : "20%"} of the recommended
                              quantity.
                            </p>
                            <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                              perfect_pick = items_within_tolerance ÷ items_adopted × 100
                            </p>
                            <p>
                              Tighter than &quot;Items the customer took&quot; - this checks both
                              the item choice and the volume. The tolerance is set per-deployment in
                              the recommendation engine config.
                            </p>
                          </div>
                        }
                      />
                    }
                    className="!border-l-brand-600"
                  />
                  <MetricCard
                    label="Sales opportunity flagged"
                    value={
                      s?.unsold_revenue != null && s.unsold_revenue > 0
                        ? fmtCurrency(s.unsold_revenue)
                        : s?.unsold_volume
                          ? `${fmtNum(s.unsold_volume)} units`
                          : "0"
                    }
                    subtitle={
                      !s || s.unsold_volume === 0
                        ? "Every recommended unit was bought"
                        : `${fmtNum(s.unsold_volume)} units across ${fmtNum(s.unsold_sku_count)} items where customers didn't buy what we suggested - opportunity to re-pitch next visit`
                    }
                    trend={s?.unsold_volume != null && s.unsold_volume > 0 ? "up" : undefined}
                    info={
                      <InfoBubble
                        title="Sales opportunity flagged"
                        body={
                          <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                            <p>
                              The AED value of items we recommended to customers that{" "}
                              <strong>they didn&apos;t buy</strong> on this visit.
                            </p>
                            <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                              unsold_per_(customer,item,day) = max(recommended_qty − bought_qty, 0)
                              <br />
                              unsold_revenue = Σ unsold × avg_unit_price
                            </p>
                            <p>
                              <strong>Read it as opportunity, not error.</strong> A high number
                              means the recommendations identified demand the customer hasn&apos;t
                              yet acted on - items the team can re-pitch on the next visit, or that
                              signal a stocking / pricing barrier.
                            </p>
                            <p>
                              This number sits alongside Dashboard&apos;s &quot;Sales opportunity
                              flagged&quot; tile - same opportunity-positive framing, scoped here to
                              recommendation-vs-purchase per customer rather than forecast-vs-sales
                              per route.
                            </p>
                          </div>
                        }
                      />
                    }
                    className="!border-l-brand-600"
                  />
                </KpiRow>

                <HighlightsStrip items={highlights} />

                <div className="space-y-6">
                  {dailyPadded.length > 0 && (
                    <LineChart
                      title="Daily share of suggestions that sold"
                      subtitle={
                        daysWithRecs > 0 && daysWithRecs < activeDays
                          ? `Recommendations stored for ${daysWithRecs} of ${activeDays} days in this window - gaps in the line mean no recommendations were generated that day.`
                          : undefined
                      }
                      data={dailyPadded as unknown as Record<string, unknown>[]}
                      xKey="date"
                      series={[
                        { key: "adoption_pct", label: "% sold", color: CHART_COLOR.success },
                      ]}
                      height={260}
                    />
                  )}

                  <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                    {data.top_over_recommended.length > 0 && (
                      <BarChart
                        title="Suggesting too much (free up van space)"
                        data={data.top_over_recommended as unknown as Record<string, unknown>[]}
                        xKey="item_code"
                        yKey="rows"
                        color={CHART_COLOR.warning}
                        height={240}
                      />
                    )}
                    {data.top_missed.length > 0 && (
                      <BarChart
                        title="Customers buying without us suggesting"
                        data={data.top_missed as unknown as Record<string, unknown>[]}
                        xKey="item_code"
                        yKey="rows"
                        color={CHART_COLOR.success}
                        height={240}
                      />
                    )}
                  </div>
                </div>
              </>
            )}
          </>
        )}
      </div>
    </Drawer>
  );
}
