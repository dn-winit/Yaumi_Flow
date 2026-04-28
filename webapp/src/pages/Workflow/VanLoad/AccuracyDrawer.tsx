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

import { useForecastRows } from "@/hooks/useDataImport";
import {
  fmtNum,
  fmtCurrency,
  toNum,
  pickDate,
  GOOD_SCORE_THRESHOLD,
  TOLERANCE_PCT,
  LEAKAGE_SHARE_WARN,
  ON_TARGET_GOOD_RATIO,
  ON_TARGET_POOR_RATIO,
  DEFAULT_LOOKBACK,
  type Lookback,
} from "@/lib/format";
import { fmtDate } from "@/lib/date";
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

  // ----- Single-pass client-side stats (forecast-dimension identity:
  // demandServed + unsoldForecast = totalPredicted). -----
  const wapeAccuracy = (absErr: number, actual: number) =>
    actual > 0 ? Math.max(0, 100 - (absErr / actual) * 100) : null;

  const stats = useMemo(() => {
    const byDay = new Map<string, { p: number; a: number }>();
    const byItem = new Map<string, { predicted: number; actual: number }>();
    let scoredAbsErr = 0;
    let scoredActual = 0;
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
      if (a > 0 && p > 0) {
        scoredAbsErr += Math.abs(a - p);
        scoredActual += a;
      }
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
      const acc = wapeAccuracy(Math.abs(p - a), a);
      if (acc != null && acc > bestDayAcc) {
        bestDayAcc = acc;
        bestDayDate = date;
      }
    });
    const bestDay = bestDayAcc >= 0 ? { date: bestDayDate, accuracy: bestDayAcc } : null;

    return {
      accuracyPct: wapeAccuracy(scoredAbsErr, scoredActual),
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
    const map = new Map<string, { predicted: number; actual: number }>();
    rows.forEach((r) => {
      const d = pickDate(r as unknown as Record<string, unknown>);
      if (!d) return;
      const predicted = toNum(r.predicted) ?? 0;
      const actual = toNum(r.actual_qty) ?? 0;
      const cur = map.get(d) ?? { predicted: 0, actual: 0 };
      cur.predicted += predicted;
      cur.actual += actual;
      map.set(d, cur);
    });
    return Array.from(map.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([date, v]) => ({
        date,
        predicted: Number(v.predicted.toFixed(2)),
        actual: Number(v.actual.toFixed(2)),
      }));
  }, [rows]);

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
    <Drawer open={open} onClose={onClose} title="Past analysis" width="xl">
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
              />
              <MetricCard
                label="Days we got it right"
                value={stats.daysScored > 0 ? `${stats.daysOnTarget} / ${stats.daysScored}` : "-"}
                subtitle={`Within ${Math.round(TOLERANCE_PCT * 100)}% of what actually sold`}
                trend={onTargetArrow}
              />
              <MetricCard
                label="Forecast that didn't sell"
                value={
                  stats.unsoldRevenue != null && stats.unsoldRevenue > 0
                    ? fmtCurrency(stats.unsoldRevenue)
                    : stats.unsoldForecast > 0
                    ? `${fmtNum(stats.unsoldForecast)} units`
                    : "0"
                }
                subtitle={
                  stats.unsoldForecast === 0
                    ? "Every forecasted unit sold"
                    : `${fmtNum(stats.unsoldForecast)} units · ${fmtNum(stats.unsoldSkuCount)} items overstocked on the van`
                }
              />
            </KpiRow>

            <HighlightsStrip items={highlights} />

            <LineChart
              title="What we forecast vs what actually sold"
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
