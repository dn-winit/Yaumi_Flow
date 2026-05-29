import { useEffect, useMemo, useState } from "react";
import Drawer from "@/components/ui/Drawer";
import EmptyState from "@/components/ui/EmptyState";
import KpiRow from "@/components/ui/KpiRow";
import MetricCard from "@/components/charts/MetricCard";
import BarChart from "@/components/charts/BarChart";
import { Skeleton } from "@/components/ui/Skeleton";
import InfoBubble from "@/components/ui/InfoBubble";
import ToggleGroup, { type ToggleOption } from "@/components/ui/ToggleGroup";
import { CHART_COLOR } from "@/components/charts/theme";
import DashboardFilterBar from "@/pages/Dashboard/DashboardFilterBar";

import { useReconciliationPastPerformance } from "@/hooks/useForecast";
import { useLastActiveDate } from "@/hooks/useDataImport";
import { fmtNum } from "@/lib/format";
import { addDays, fmtDate, fmtDateRange, todayIso } from "@/lib/date";
import type { DashboardFilters, ReportingPeriod } from "@/types/data-import";
import { EMPTY_FILTERS } from "@/types/data-import";

type ChartView = "van_load" | "leftovers";

const CHART_TOGGLE_OPTIONS: ToggleOption<ChartView>[] = [
  { value: "van_load", label: "Van load" },
  { value: "leftovers", label: "Leftovers" },
];

const CHART_SERIES_VAN_LOAD = [
  { key: "rep_van_load", label: "Actual van load", color: CHART_COLOR.warning },
  { key: "recommended_van_load", label: "Recommended van load", color: CHART_COLOR.brandPrimary },
  { key: "actual_sold", label: "Actually sold", color: CHART_COLOR.success },
];

const CHART_SERIES_LEFTOVERS = [
  { key: "actual_leftover", label: "Actual leftover", color: CHART_COLOR.warning },
  { key: "recommended_leftover", label: "Recommended leftover", color: CHART_COLOR.brandPrimary },
];

interface Props {
  open: boolean;
  onClose: () => void;
  routeCode?: string;
  /** Last day of the window; defaults to "yesterday" when caller has no date in scope. */
  endDate?: string;
}

/** Past-performance drawer; field semantics in src/types/forecast.ts. */
export default function AccuracyDrawer({ open, onClose, routeCode, endDate: endDateProp }: Props) {
  // Anchor on the data's actual last active date so the drawer never opens on a zero-data day.
  const { date: lastActiveDate, loading: lastActiveLoading } = useLastActiveDate();

  // period stays null until lastActiveDate resolves -- prevents flicker against a calendar default.
  const [period, setPeriod] = useState<ReportingPeriod | null>(null);
  const [filters, setFilters] = useState<DashboardFilters>(EMPTY_FILTERS);

  // (Re)seed period when the drawer opens, route changes, parent end-date moves, or last active shifts.
  // Window ends at min(endDateProp - 1, lastActiveDate) so it never overlaps the planning day,
  // never crosses into the future, and never lands past the CSV. Falls back to today if CSV is empty
  // so the inner empty-state can surface the right copy.
  useEffect(() => {
    if (!open) return;
    if (lastActiveLoading) return;
    setFilters({
      ...EMPTY_FILTERS,
      route_codes: routeCode ? [routeCode] : [],
    });
    const anchor = lastActiveDate ?? todayIso();
    const dayBeforeSelected = endDateProp ? addDays(endDateProp, -1) : null;
    const end = dayBeforeSelected && dayBeforeSelected < anchor ? dayBeforeSelected : anchor;
    setPeriod({ start_date: addDays(end, -29), end_date: end });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, routeCode, endDateProp, lastActiveDate, lastActiveLoading]);

  const apiFilters = useMemo(
    () => ({
      item_codes: filters.item_codes,
      category_codes: filters.category_codes,
    }),
    [filters.item_codes, filters.category_codes],
  );

  const { data, loading } = useReconciliationPastPerformance(
    routeCode,
    open && period ? period.start_date : undefined,
    open && period ? period.end_date : undefined,
    open && Boolean(routeCode) && period != null,
    apiFilters,
  );

  return (
    <Drawer open={open} onClose={onClose} title="Past performance" width="xl">
      <div className="space-y-6">
        {period == null ? (
          <DrawerSkeleton />
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
            {data?.active_days != null && (
              <div className="text-caption text-text-tertiary">
                {data.active_days} active day
                {data.active_days === 1 ? "" : "s"} in window
              </div>
            )}

            {loading || lastActiveLoading ? (
              <DrawerSkeleton />
            ) : !data?.available || data.daily.length === 0 ? (
              <EmptyState
                title={
                  period.start_date === period.end_date
                    ? `No activity on ${fmtDate(period.end_date)}`
                    : "No past activity for this window"
                }
                message={
                  data?.message ??
                  (period.start_date === period.end_date
                    ? `Route ${routeCode ?? ""} had no allocation, sales, or returns on this day. Most likely a weekend, holiday, or non-route day. Try the previous working day.`
                    : `No allocation or sales recorded for route ${routeCode ?? ""} in this period.`)
                }
              />
            ) : (
              <>
                <DrawerContent data={data} />
              </>
            )}
          </>
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

// Pure render: server pre-computes everything; client only formats + maps colour tokens.
function DrawerContent({ data }: ContentProps) {
  const t = data.totals;

  // Period label: single date or date range, mirroring the server's window.
  const windowLabel = (() => {
    const start = data.start_date ?? "";
    const end = data.end_date ?? "";
    if (!start && !end) return null;
    if (!start || !end || start === end) return fmtDate(end || start);
    return fmtDateRange(start, end);
  })();

  const num = (value: number) => <span className="tabular-nums">{fmtNum(value)}</span>;

  const items = data.items ?? [];
  const categories = data.categories ?? [];

  const [chartView, setChartView] = useState<ChartView>("van_load");
  const chartSeries = useMemo(
    () => (chartView === "van_load" ? CHART_SERIES_VAN_LOAD : CHART_SERIES_LEFTOVERS),
    [chartView],
  );
  const chartSubtitle =
    chartView === "van_load"
      ? "Actual van load (yellow), recommended van load (red), actually sold (green)"
      : "Actual leftover (yellow) vs recommended leftover (red) per day";

  return (
    <>
      {windowLabel && (
        <div className="rounded-lg border border-default bg-surface-sunken px-4 py-2.5">
          <p className="text-body text-text-secondary">
            Showing results for <strong className="text-text-primary">{windowLabel}</strong>
          </p>
        </div>
      )}

      {/* Row 1 hero tiles; border colour matches the chart bar below. */}
      <KpiRow>
        <MetricCard
          label="Actual van load"
          value={
            <span className="inline-flex items-baseline gap-1.5">
              {num(t.rep_van_load_total)}
              <span className="text-body font-normal text-text-tertiary">units</span>
            </span>
          }
          info={
            <InfoBubble
              title="What is actual van load?"
              body={
                <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                  <p>
                    The total stock the rep physically loaded onto the truck across every working
                    day in this window.
                  </p>
                  <p>
                    For each item on each day, this is what was carried from yesterday plus what the
                    depot issued today:
                  </p>
                  <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                    actual van load = yesterday&apos;s leftover + today&apos;s depot allocation
                  </p>
                  <p>
                    Both numbers come from the SQL Server views that record the rep&apos;s real
                    truck activity (closing stock for the carry, depot allocation for the fresh
                    issue).
                  </p>
                </div>
              }
            />
          }
          className="!border-l-warning-500"
        />
        <MetricCard
          label="Recommended van load"
          value={
            <span className="inline-flex items-baseline gap-1.5">
              {num(t.recommended_van_load_total)}
              <span className="text-body font-normal text-text-tertiary">units</span>
            </span>
          }
          info={
            <InfoBubble
              title="What is recommended van load?"
              body={
                <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                  <p>
                    What our engine thinks the rep should have loaded onto the truck across the same
                    window.
                  </p>
                  <p>
                    For each item on each day, this is the carry from yesterday plus the fresh stock
                    our forecast says the depot should issue:
                  </p>
                  <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                    recommended van load = carry + fresh from depot
                  </p>
                  <p>
                    The fresh part is sized so the truck just covers expected demand without
                    over-stocking:
                  </p>
                  <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                    fresh from depot = max(0, expected demand &minus; carry)
                  </p>
                  <p>
                    Same number rendered on the Van Load page&apos;s headline tile -- one
                    recommendation, one definition.
                  </p>
                </div>
              }
            />
          }
          className="!border-l-brand-600"
        />
        <MetricCard
          label="Actually sold"
          value={
            <span className="inline-flex items-baseline gap-1.5">
              {num(t.actual_sold_total)}
              <span className="text-body font-normal text-text-tertiary">units</span>
            </span>
          }
          info={
            <InfoBubble
              title="What does 'actually sold' mean?"
              body={
                <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                  <p>
                    The total units the rep invoiced to customers across this window -- the ground
                    truth we&apos;re scoring both policies against.
                  </p>
                  <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                    sum(TotalQuantity) where TrxType = &apos;SalesInvoice&apos;
                    <br />
                    and ItemType = &apos;OrderItem&apos;
                  </p>
                  <p>
                    Returns (good or bad) are tracked separately and are <strong>not</strong>{" "}
                    counted as sales here. Same source the dashboard uses, so the two surfaces
                    always agree.
                  </p>
                </div>
              }
            />
          }
          className="!border-l-success-600"
        />
      </KpiRow>

      {/* Row 2: SKU coverage (forecast scope) + leftovers saved (load shape). */}
      <KpiRow>
        <MetricCard
          label="SKU coverage"
          value={
            t.skus_sold > 0 ? (
              <span className="inline-flex items-baseline gap-1.5">
                <span className="tabular-nums">
                  {t.skus_covered}
                  <span className="text-text-tertiary"> / {t.skus_sold}</span>
                </span>
                <span className="text-body font-normal text-text-tertiary">
                  ({t.skus_coverage_pct}%)
                </span>
              </span>
            ) : (
              <span className="text-text-tertiary">-</span>
            )
          }
          subtitle="SKUs covered by our recommendation, out of SKUs the rep actually sold"
          info={
            <InfoBubble
              title="What is SKU coverage?"
              body={
                <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                  <p>
                    Of the distinct items the rep actually sold in this window, how many did our
                    engine also forecast?
                  </p>
                  <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                    SKU coverage = items we forecasted that sold &divide; items the rep sold
                  </p>
                  <p>
                    100% means we predicted demand for every item the rep moved. A lower number
                    reveals <strong>blind spots</strong> -- items the rep needed that we didn&apos;t
                    anticipate.
                  </p>
                  <p>
                    Both sides are <strong>distinct item counts</strong>, not unit totals -- so a
                    one-off sale of a slow item weighs the same as a high-volume item. That keeps
                    the metric about <em>breadth of forecast scope</em>, not size of any one item.
                  </p>
                </div>
              }
            />
          }
          className="!border-l-brand-600"
        />
        <MetricCard
          label="Leftovers saved"
          value={
            t.rep_leftover_units > 0 ? (
              <span className="inline-flex items-baseline gap-1.5">
                {num(t.leftover_units_saved)}
                <span className="text-body font-normal text-text-tertiary">
                  units ({t.leftover_pct_saved}%)
                </span>
              </span>
            ) : (
              <span className="text-text-tertiary">0 units</span>
            )
          }
          subtitle={
            <span>
              Rep had {fmtNum(t.rep_leftover_units)} leftover; we'd have{" "}
              {fmtNum(t.our_leftover_units)}
            </span>
          }
          info={
            <InfoBubble
              title="What does 'leftovers saved' mean?"
              body={
                <div className="space-y-3 text-body text-text-secondary leading-relaxed">
                  <p>
                    The difference between what the rep had to carry overnight versus what our
                    policy would have left over -- across every item, every working day in the
                    window.
                  </p>
                  <p>
                    For each item on each day, leftover is the stock that didn&apos;t sell that day:
                  </p>
                  <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                    leftover = max(0, load &minus; sold)
                  </p>
                  <p>
                    Items that ran short (sold more than loaded) are <strong>stock-outs</strong>, a
                    different failure mode -- not counted as leftovers. That&apos;s why the formula
                    is bounded at 0.
                  </p>
                  <p>Then:</p>
                  <p className="font-mono text-caption bg-surface-sunken p-3 rounded">
                    leftovers saved = rep&apos;s leftover total &minus; our leftover total
                  </p>
                  <p>
                    A positive number means our policy leaves <strong>less stock overnight</strong>{" "}
                    -- leaner truck, less stale inventory, less handling. Negative means we&apos;d
                    have left more (the tile flips to amber + down-arrow when that happens).
                  </p>
                </div>
              }
            />
          }
          trend={
            t.leftover_units_saved > 0 ? "up" : t.leftover_units_saved < 0 ? "down" : undefined
          }
          className={
            t.leftover_units_saved >= 0 ? "!border-l-success-600" : "!border-l-warning-500"
          }
        />
      </KpiRow>

      {/* Category + item drilldowns; server-sorted, collapsed by default. */}
      {categories.length > 0 && (
        <details className="rounded-lg border border-default bg-surface-raised">
          <summary className="cursor-pointer select-none px-4 py-3 text-body font-medium text-text-primary hover:bg-surface-sunken/40">
            Category breakdown ({categories.length}{" "}
            {categories.length === 1 ? "category" : "categories"})
          </summary>
          <div className="max-h-[480px] overflow-auto border-t border-default">
            <table className="w-full text-body">
              <thead className="sticky top-0 z-10 bg-surface-sunken">
                <tr className="text-left">
                  <th className="px-4 py-2 text-caption font-semibold uppercase tracking-wider text-text-secondary">
                    Category
                  </th>
                  <th className="px-4 py-2 text-right text-caption font-semibold uppercase tracking-wider text-text-secondary">
                    SKUs
                  </th>
                  <th className="px-4 py-2 text-right text-caption font-semibold uppercase tracking-wider text-text-secondary">
                    Actual van load
                  </th>
                  <th className="px-4 py-2 text-right text-caption font-semibold uppercase tracking-wider text-text-secondary">
                    Recommended van load
                  </th>
                  <th className="px-4 py-2 text-right text-caption font-semibold uppercase tracking-wider text-text-secondary">
                    Actually sold
                  </th>
                  <th className="px-4 py-2 text-right text-caption font-semibold uppercase tracking-wider text-text-secondary">
                    Actual leftover
                  </th>
                  <th className="px-4 py-2 text-right text-caption font-semibold uppercase tracking-wider text-text-secondary">
                    Recommended leftover
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-subtle">
                {categories.map((c) => (
                  <tr key={c.categoryName} className="hover:bg-surface-sunken/40">
                    <td className="px-4 py-2 align-top font-medium text-text-primary">
                      {c.categoryName}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-right align-top tabular-nums">
                      {c.skus}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-right align-top tabular-nums">
                      {fmtNum(c.rep_van_load)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-right align-top tabular-nums">
                      {fmtNum(c.recommended_van_load)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-right align-top tabular-nums">
                      {fmtNum(c.actual_sold)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-right align-top tabular-nums">
                      {fmtNum(c.actual_leftover)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-right align-top tabular-nums">
                      {fmtNum(c.recommended_leftover)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}

      {items.length > 0 && (
        <details className="rounded-lg border border-default bg-surface-raised">
          <summary className="cursor-pointer select-none px-4 py-3 text-body font-medium text-text-primary hover:bg-surface-sunken/40">
            Item-by-item breakdown ({items.length} rows)
          </summary>
          <div className="max-h-[480px] overflow-auto border-t border-default">
            <table className="w-full text-body">
              <thead className="sticky top-0 z-10 bg-surface-sunken">
                <tr className="text-left">
                  <th className="px-4 py-2 text-caption font-semibold uppercase tracking-wider text-text-secondary">
                    Date
                  </th>
                  <th className="px-4 py-2 text-caption font-semibold uppercase tracking-wider text-text-secondary">
                    Item
                  </th>
                  <th className="px-4 py-2 text-caption font-semibold uppercase tracking-wider text-text-secondary">
                    Category
                  </th>
                  <th className="px-4 py-2 text-right text-caption font-semibold uppercase tracking-wider text-text-secondary">
                    Actual van load
                  </th>
                  <th className="px-4 py-2 text-right text-caption font-semibold uppercase tracking-wider text-text-secondary">
                    Recommended van load
                  </th>
                  <th className="px-4 py-2 text-right text-caption font-semibold uppercase tracking-wider text-text-secondary">
                    Actually sold
                  </th>
                  <th className="px-4 py-2 text-right text-caption font-semibold uppercase tracking-wider text-text-secondary">
                    Actual leftover
                  </th>
                  <th className="px-4 py-2 text-right text-caption font-semibold uppercase tracking-wider text-text-secondary">
                    Recommended leftover
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-subtle">
                {items.map((r) => (
                  <tr key={`${r.date}-${r.itemCode}`} className="hover:bg-surface-sunken/40">
                    <td className="whitespace-nowrap px-4 py-2 align-top tabular-nums text-text-secondary">
                      {fmtDate(r.date)}
                    </td>
                    <td className="px-4 py-2 align-top">
                      <div className="truncate font-medium text-text-primary" title={r.itemName}>
                        {r.itemName}
                      </div>
                      <div className="mt-0.5 text-caption uppercase tracking-wider text-text-tertiary">
                        {r.itemCode}
                      </div>
                    </td>
                    <td className="px-4 py-2 align-top text-text-secondary">
                      {r.categoryName || <span className="text-text-tertiary">Uncategorised</span>}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-right align-top tabular-nums">
                      {fmtNum(r.rep_van_load)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-right align-top tabular-nums">
                      {fmtNum(r.recommended_van_load)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-right align-top tabular-nums">
                      {fmtNum(r.actual_sold)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-right align-top tabular-nums">
                      {fmtNum(r.actual_leftover)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-right align-top tabular-nums">
                      {fmtNum(r.recommended_leftover)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}

      {/* Grouped bar chart -- toggle between Van load (3 bars) and Leftovers (2 bars). */}
      <BarChart
        title="Day-by-day comparison"
        subtitle={chartSubtitle}
        actions={
          <ToggleGroup
            options={CHART_TOGGLE_OPTIONS}
            value={chartView}
            onChange={setChartView}
            ariaLabel="Chart view"
          />
        }
        data={data.daily as unknown as Record<string, unknown>[]}
        xKey="date"
        series={chartSeries}
        height={320}
      />
    </>
  );
}
