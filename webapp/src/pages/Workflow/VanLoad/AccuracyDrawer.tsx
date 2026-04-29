import { useEffect, useMemo, useState } from "react";
import Drawer from "@/components/ui/Drawer";
import Loading from "@/components/ui/Loading";
import EmptyState from "@/components/ui/EmptyState";
import KpiRow from "@/components/ui/KpiRow";
import HighlightsStrip, { type Highlight } from "@/components/ui/HighlightsStrip";
import MetricCard from "@/components/charts/MetricCard";
import LineChart from "@/components/charts/LineChart";
import BarChart from "@/components/charts/BarChart";
import { CHART_COLOR } from "@/components/charts/theme";
import ExplainabilityModal from "@/components/ui/ExplainabilityModal";
import DashboardFilterBar from "@/pages/Dashboard/DashboardFilterBar";

import { useForecastRows, useLookbackWindow } from "@/hooks/useDataImport";
import {
  fmtNum,
  fmtCurrency,
  toNum,
  pickDate,
  compositeAccuracy,
  GOOD_SCORE_THRESHOLD,
  TOLERANCE_PCT,
  LEAKAGE_SHARE_WARN,
  ON_TARGET_GOOD_RATIO,
  ON_TARGET_POOR_RATIO,
  DEFAULT_LOOKBACK,
  type Lookback,
} from "@/lib/format";
import { fmtDate } from "@/lib/date";
import InfoBubble from "@/components/ui/InfoBubble";
import ForecastAccuracyExplanation from "@/components/ui/ForecastAccuracyExplanation";
import type { DashboardFilters, ForecastRow } from "@/types/data-import";
import { EMPTY_FILTERS } from "@/types/data-import";
import type { Row } from "@/types/common";

interface VarianceRow {
  item_code: string;
  predicted: number;
  actual: number;
  variance: number;
}

interface Props {
  open: boolean;
  onClose: () => void;
  // The drawer is opened from a specific route's van-load context, so the
  // route is fixed and pre-applied. The user can further narrow inside
  // the drawer by category and item. Warehouse + route would be redundant
  // here so the filter bar hides them.
  routeCode?: string;
}

/**
 * "Past analysis" drawer: predicted vs actual for the route the user is
 * viewing, scoped further by Category and Item via the same multi-select
 * cascading filter bar the dashboard uses. The four tiles, daily trend
 * chart, and items-variance bar chart are computed client-side from the
 * shared (sales ⋈ forecast) merge served by /eda/forecast-rows -- the
 * same merge that powers the dashboard KPIs, so numbers reconcile.
 */
export default function AccuracyDrawer({ open, onClose, routeCode }: Props) {
  const [explainRow, setExplainRow] = useState<Row | null>(null);
  const [lookback, setLookback] = useState<Lookback>(DEFAULT_LOOKBACK);
  const [filters, setFilters] = useState<DashboardFilters>(EMPTY_FILTERS);

  // Seed the drawer's filter scope with the active route on every open so
  // the user always lands on "this route's history". The route field is
  // hidden in the filter bar but still pinned in state.
  useEffect(() => {
    if (!open) return;
    setFilters({
      ...EMPTY_FILTERS,
      route_codes: routeCode ? [routeCode] : [],
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, routeCode]);

  const { data, loading } = useForecastRows(
    open ? lookback : undefined,
    open ? filters : undefined,
    open,
  );
  const rows = (data?.rows ?? []) as ForecastRow[];

  // Working-day axis for the daily chart -- pads gaps when scope filters
  // strip a day from the merge.
  const windowQ = useLookbackWindow(open ? lookback : undefined);
  const activeDates = windowQ.data?.active_dates ?? [];

  // Single-pass aggregation. Headline accuracyPct uses the canonical
  // composite helper so it reconciles with the Pipeline tiles.
  const stats = useMemo(() => {
    const byDay = new Map<string, { p: number; a: number }>();
    const byItem = new Map<string, { predicted: number; actual: number }>();
    let totalActual = 0;
    let totalPredicted = 0;
    let demandServed = 0;
    let unsoldForecast = 0;
    let unsoldRevenue = 0;
    let pricesSeen = false;

    rows.forEach((r) => {
      const p = toNum(r.predicted) ?? 0;
      const a = toNum(r.actual_qty) ?? 0;
      totalActual += a;
      totalPredicted += p;
      demandServed += Math.min(p, a);
      const excess = Math.max(0, p - a);
      if (excess > 0) {
        unsoldForecast += excess;
        const price = r.price ?? 0;
        if (price > 0) {
          unsoldRevenue += excess * price;
          pricesSeen = true;
        }
      }

      const d = pickDate(r as unknown as Record<string, unknown>);
      if (d) {
        const cur = byDay.get(d) ?? { p: 0, a: 0 };
        cur.p += p;
        cur.a += a;
        byDay.set(d, cur);
      }
      const code = String(r.item_code ?? "");
      if (code) {
        const agg = byItem.get(code) ?? { predicted: 0, actual: 0 };
        agg.predicted += p;
        agg.actual += a;
        byItem.set(code, agg);
      }
    });

    const accuracyPct = compositeAccuracy(
      rows.map((r) => ({
        predicted: toNum(r.predicted) ?? 0,
        actual: toNum(r.actual_qty) ?? 0,
        demandClass: r.demand_class,
      })),
    );

    let forecastedSkuCount = 0;
    let servedSkuCount = 0;
    let unsoldSkuCount = 0;
    byItem.forEach(({ predicted, actual }) => {
      if (predicted > 0) forecastedSkuCount += 1;
      if (predicted > 0 && actual > 0) servedSkuCount += 1;
      if (predicted > actual) unsoldSkuCount += 1;
    });

    const dayEntries = Array.from(byDay.entries())
      .filter(([, v]) => v.a > 0 && v.p > 0)
      .sort(([a], [b]) => a.localeCompare(b));

    let daysOnTarget = 0;
    let bestStreak = 0;
    let currentStreak = 0;
    let bestDayDate = "";
    let bestDayAcc = -1;

    dayEntries.forEach(([date, { p, a }]) => {
      if (Math.abs(p - a) / a <= TOLERANCE_PCT) {
        daysOnTarget += 1;
        currentStreak += 1;
        if (currentStreak > bestStreak) bestStreak = currentStreak;
      } else {
        currentStreak = 0;
      }
      // Plain day-level WAPE for the "best day" highlight.
      const acc = a > 0 ? Math.max(0, 100 - (Math.abs(p - a) / a) * 100) : null;
      if (acc != null && acc > bestDayAcc) {
        bestDayAcc = acc;
        bestDayDate = date;
      }
    });
    const bestDay = bestDayAcc >= 0 ? { date: bestDayDate, accuracy: bestDayAcc } : null;

    return {
      accuracyPct,
      demandServed,
      totalActual,
      totalPredicted,
      forecastedSkuCount,
      servedSkuCount,
      daysOnTarget,
      daysScored: dayEntries.length,
      bestStreak,
      bestDay,
      unsoldForecast,
      unsoldSkuCount,
      unsoldRevenue: pricesSeen ? unsoldRevenue : null,
    };
  }, [rows]);

  const dailyChart = useMemo(() => {
    // Track whether each date had ANY genuine prediction contribution. The
    // model only emits predictions for the test horizon (last 30 days from
    // training cutoff) plus the forward forecast horizon -- dates outside
    // those windows return 0 predicted from the merge, which is "no model
    // coverage", not "model said zero". Showing 0 there flat-lines the
    // forecast series misleadingly. Carry a flag so the renderer can pad
    // those days with null and let the line break at the gap.
    const map = new Map<string, { predicted: number; actual: number; hasPrediction: boolean }>();
    rows.forEach((r) => {
      const d = pickDate(r as unknown as Record<string, unknown>);
      if (!d) return;
      const predicted = toNum(r.predicted) ?? 0;
      const actual = toNum(r.actual_qty) ?? 0;
      const cur = map.get(d) ?? { predicted: 0, actual: 0, hasPrediction: false };
      cur.predicted += predicted;
      cur.actual += actual;
      if (predicted > 0) cur.hasPrediction = true;
      map.set(d, cur);
    });
    // Pad against the canonical working-day list so the X-axis always
    // shows every working day in the window. Falls back to the dates
    // that have data if active_dates is still loading.
    const axis = activeDates.length > 0
      ? activeDates
      : Array.from(map.keys()).sort();
    return axis.map((date) => {
      const v = map.get(date);
      if (!v) {
        // Day with neither sale nor forecast -- both are absent.
        return { date, predicted: null as unknown as number, actual: null as unknown as number };
      }
      return {
        date,
        predicted: v.hasPrediction
          ? Number(v.predicted.toFixed(2))
          : (null as unknown as number),
        actual: Number(v.actual.toFixed(2)),
      };
    });
  }, [rows, activeDates]);

  // Coverage diagnostic for the chart subtitle: how many of the rendered
  // days actually carry a model prediction.
  const daysWithPrediction = useMemo(
    () => dailyChart.filter((d) => d.predicted != null).length,
    [dailyChart],
  );

  const itemVarianceChart = useMemo(() => {
    const byItem = new Map<string, { predicted: number; actual: number }>();
    rows.forEach((r) => {
      const code = String(r.item_code ?? "");
      if (!code) return;
      const predicted = toNum(r.predicted) ?? 0;
      const actual = toNum(r.actual_qty) ?? 0;
      const cur = byItem.get(code) ?? { predicted: 0, actual: 0 };
      cur.predicted += predicted;
      cur.actual += actual;
      byItem.set(code, cur);
    });
    return Array.from(byItem.entries())
      // An item with predicted == 0 is "no model coverage" (e.g. a SKU
      // added after the training cut-off). Counting it as a forecast miss
      // would surface it as the worst variance even though the model was
      // never asked. Restrict the chart to items the model actually
      // forecast so every bar is a genuine miss the team can act on.
      .filter(([, v]) => v.predicted > 0)
      .map(([item_code, v]) => ({
        item_code,
        predicted: Number(v.predicted.toFixed(1)),
        actual: Number(v.actual.toFixed(1)),
        variance: Number((v.actual - v.predicted).toFixed(1)),
      }))
      .sort((a, b) => Math.abs(b.variance) - Math.abs(a.variance))
      .slice(0, 10);
  }, [rows]);

  const mostAccurateItem = useMemo(() => {
    const significantVolume = stats.totalActual * LEAKAGE_SHARE_WARN;
    let bestCode = "";
    let bestErr = Number.POSITIVE_INFINITY;
    itemVarianceChart.forEach((r) => {
      if (r.actual <= 0 || r.actual < significantVolume) return;
      const errPct = Math.abs(r.variance) / r.actual;
      if (errPct < bestErr) {
        bestErr = errPct;
        bestCode = r.item_code;
      }
    });
    return bestCode ? { item: bestCode, errPct: bestErr } : null;
  }, [itemVarianceChart, stats.totalActual]);

  const highlights = useMemo(() => {
    const items: Highlight[] = [];
    if (stats.bestDay) {
      items.push({
        label: "Best day",
        value: `${stats.bestDay.accuracy.toFixed(1)}% accurate`,
        detail: fmtDate(stats.bestDay.date),
      });
    }
    if (stats.bestStreak > 0) {
      items.push({
        label: "Best streak",
        value: `${stats.bestStreak} day${stats.bestStreak === 1 ? "" : "s"} on target`,
        detail: `within ${Math.round(TOLERANCE_PCT * 100)}% of actual`,
      });
    }
    if (mostAccurateItem) {
      items.push({
        label: "Most accurate item",
        value: mostAccurateItem.item,
        detail: `${(mostAccurateItem.errPct * 100).toFixed(1)}% off across the window`,
      });
    }
    return items;
  }, [stats.bestDay, stats.bestStreak, mostAccurateItem]);

  const servedArrow: "up" | "down" | undefined = stats.demandServed > 0 ? "up" : undefined;
  const accuracyArrow: "up" | "down" | undefined =
    stats.accuracyPct == null
      ? undefined
      : stats.accuracyPct >= GOOD_SCORE_THRESHOLD
      ? "up"
      : "down";
  const onTargetRatio =
    stats.daysScored > 0 ? stats.daysOnTarget / stats.daysScored : null;
  const onTargetArrow: "up" | "down" | undefined =
    onTargetRatio == null
      ? undefined
      : onTargetRatio >= ON_TARGET_GOOD_RATIO
      ? "up"
      : onTargetRatio < ON_TARGET_POOR_RATIO
      ? "down"
      : undefined;

  const windowLabel = data?.working_days
    ? `${data.working_days} working day${data.working_days === 1 ? "" : "s"}`
    : "this period";

  return (
    <Drawer open={open} onClose={onClose} title="Past performance — forecast vs actual" width="xl">
      <div className="space-y-6">
        <DashboardFilterBar
          value={filters}
          onChange={setFilters}
          lookback={lookback}
          onLookbackChange={setLookback}
          hideWarehouse
          hideRoute
        />

        {loading ? (
          <Loading message="Loading past analysis..." />
        ) : rows.length === 0 ? (
          <EmptyState
            title="No historical data"
            message={
              data?.message ??
              `No predictions matched actuals for route ${routeCode ?? "this scope"} in ${windowLabel}.`
            }
          />
        ) : (
          <>
            <KpiRow>
              <MetricCard
                label="Demand served by our forecast"
                value={`${fmtNum(stats.demandServed)} units`}
                subtitle={
                  stats.totalPredicted > 0
                    ? `${fmtNum(stats.demandServed)} of ${fmtNum(stats.totalPredicted)} forecast units sold · ${fmtNum(stats.servedSkuCount)} items`
                    : "No forecast in this window"
                }
                trend={servedArrow}
              />
              <MetricCard
                label="Forecast accuracy"
                value={stats.accuracyPct != null ? `${stats.accuracyPct.toFixed(1)}%` : "-"}
                subtitle={
                  stats.daysScored > 0
                    ? `${stats.daysScored} of ${windowLabel} with records`
                    : `No records in ${windowLabel}`
                }
                trend={accuracyArrow}
                info={
                  <InfoBubble
                    title="How forecast accuracy is calculated"
                    body={<ForecastAccuracyExplanation />}
                  />
                }
              />
              <MetricCard
                label="Days we got it right"
                value={stats.daysScored > 0 ? `${stats.daysOnTarget} / ${stats.daysScored}` : "-"}
                subtitle={`Within ${Math.round(TOLERANCE_PCT * 100)}% of what actually sold`}
                trend={onTargetArrow}
              />
              <MetricCard
                label="Sales we could have made"
                value={
                  stats.unsoldRevenue != null && stats.unsoldRevenue > 0
                    ? fmtCurrency(stats.unsoldRevenue)
                    : stats.unsoldForecast > 0
                    ? `${fmtNum(stats.unsoldForecast)} units`
                    : "0"
                }
                subtitle={
                  stats.unsoldForecast === 0
                    ? "We captured every forecasted unit"
                    : `${fmtNum(stats.unsoldForecast)} units across ${fmtNum(stats.unsoldSkuCount)} items — extra sales the forecast pointed to`
                }
              />
            </KpiRow>

            <HighlightsStrip items={highlights} />

            <LineChart
              title="What we forecast vs what actually sold"
              subtitle={
                daysWithPrediction > 0 && daysWithPrediction < dailyChart.length
                  ? `Model predictions cover ${daysWithPrediction} of ${dailyChart.length} days in this window — gaps in the forecast line mean the date falls outside the model's test horizon, so no prediction was generated for it.`
                  : undefined
              }
              data={dailyChart}
              xKey="date"
              series={[
                { key: "predicted", label: "Forecast" },
                { key: "actual", label: "Actual" },
              ]}
              height={300}
            />

            {itemVarianceChart.length > 0 && (
              <BarChart
                title="Items where forecast missed the mark"
                data={itemVarianceChart}
                xKey="item_code"
                yKey="variance"
                color={CHART_COLOR.warning}
                height={260}
                onBarClick={(p) => {
                  const v = p as unknown as VarianceRow;
                  setExplainRow({
                    item_code: v.item_code,
                    route_code: routeCode,
                    predicted: v.predicted,
                    actual_qty: v.actual,
                  });
                }}
              />
            )}
          </>
        )}
      </div>

      <ExplainabilityModal
        open={explainRow != null}
        onClose={() => setExplainRow(null)}
        row={explainRow}
      />
    </Drawer>
  );
}
