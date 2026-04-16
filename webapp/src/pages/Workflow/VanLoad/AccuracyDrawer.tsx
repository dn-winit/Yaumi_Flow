import { useMemo, useState } from "react";
import Drawer from "@/components/ui/Drawer";
import Loading from "@/components/ui/Loading";
import EmptyState from "@/components/ui/EmptyState";
import DrawerContextBar from "@/components/ui/DrawerContextBar";
import KpiRow from "@/components/ui/KpiRow";
import HighlightsStrip, { type Highlight } from "@/components/ui/HighlightsStrip";
import MetricCard from "@/components/charts/MetricCard";
import LineChart from "@/components/charts/LineChart";
import BarChart from "@/components/charts/BarChart";
import { CHART_COLOR } from "@/components/charts/theme";
import ExplainabilityModal from "@/components/ui/ExplainabilityModal";

import { useAccuracyComparison } from "@/hooks/useForecast";
import {
  fmtNum,
  toNum,
  GOOD_SCORE_THRESHOLD,
  TOLERANCE_PCT,
  BIAS_GOOD_PCT,
  BIAS_WARN_PCT,
  LEAKAGE_SHARE_WARN,
  TREND_STEP_PP,
} from "@/lib/format";
import { addDays, todayIso } from "@/lib/date";
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
  routeCode?: string;
  itemCodes?: string[];
}

export default function AccuracyDrawer({ open, onClose, routeCode, itemCodes }: Props) {
  const [explainRow, setExplainRow] = useState<Row | null>(null);
  const params = useMemo(() => {
    const endDate = todayIso();
    // Inclusive 30-day window: today + previous 29 days = 30 calendar days.
    const startDate = addDays(endDate, -29);
    const p: Record<string, unknown> = {
      start_date: startDate,
      end_date: endDate,
    };
    if (routeCode) p.route_code = routeCode;
    // Only push item_code to the API when a single SKU is picked. With 2+ SKUs
    // the API can only filter one, so we fetch the route-wide window and filter
    // client-side below; the backend summary in that case isn't useful.
    if (itemCodes && itemCodes.length === 1) p.item_code = itemCodes[0];
    return p;
  }, [routeCode, itemCodes]);

  // Only run the cross-DB join when the drawer is actually open.
  const { data, loading } = useAccuracyComparison(params, open);

  const multiItemFilter = !!(itemCodes && itemCodes.length > 1);

  const filteredRows = useMemo(() => {
    const rows = (data?.rows ?? []) as unknown as Row[];
    if (!multiItemFilter) return rows;
    const set = new Set(itemCodes);
    return rows.filter((r) => set.has(String(r.item_code ?? "")));
  }, [data, itemCodes, multiItemFilter]);

  // Single-pass client-side stats.
  //
  // Accuracy uses WAPE (weighted absolute percentage error) = 100 - sum|err| /
  // sum(actual). Robust to spike days: a day with a tiny actual can't explode
  // into a 9,900% per-day error the way plain MAPE would -- errors contribute
  // proportionally to the business volume that produced them.
  //
  // Derivatives surfaced so the scorecard + highlights strip + opportunities
  // section all read from the same memoised object:
  //   * daysOnTarget / daysScored       -- count of days within TOLERANCE_PCT
  //   * trendPP                          -- late-window WAPE-acc minus early
  //   * bestDay                          -- the most-accurate scored day
  //   * bestStreak                       -- longest run of consecutive on-target days
  const wapeAccuracy = (absErr: number, actual: number) =>
    actual > 0 ? Math.max(0, 100 - (absErr / actual) * 100) : null;

  const stats = useMemo(() => {
    const byDay = new Map<string, { p: number; a: number }>();
    let totalPred = 0;
    let totalActual = 0;
    let scoredAbsErr = 0;
    let scoredActual = 0;

    filteredRows.forEach((r) => {
      const p = toNum(r.predicted) ?? 0;
      const a = toNum(r.actual_qty) ?? 0;
      totalPred += p;
      totalActual += a;
      // WAPE numerator/denominator: only rows where actual > 0, matching backend
      if (a > 0) {
        scoredAbsErr += Math.abs(a - p);
        scoredActual += a;
      }
      const d = String(r.trx_date ?? "").slice(0, 10);
      if (d) {
        const cur = byDay.get(d) ?? { p: 0, a: 0 };
        cur.p += p;
        cur.a += a;
        byDay.set(d, cur);
      }
    });

    // Sort days chronologically so streaks + trend are well-defined.
    const dayEntries = Array.from(byDay.entries())
      .filter(([, v]) => v.a > 0)
      .sort(([a], [b]) => a.localeCompare(b));

    let daysOnTarget = 0;
    let bestStreak = 0;
    let currentStreak = 0;
    let bestDayDate = "";
    let bestDayAcc = -1;

    dayEntries.forEach(([date, { p, a }]) => {
      const onTarget = Math.abs(p - a) / a <= TOLERANCE_PCT;
      if (onTarget) {
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

    // Trend: compare the late half of the window against the early half using
    // the same WAPE formula. Needs >= 2 scored days to be meaningful.
    let trendPP: number | null = null;
    if (dayEntries.length >= 2) {
      const mid = Math.floor(dayEntries.length / 2);
      const halfAcc = (slice: typeof dayEntries) => {
        let err = 0, act = 0;
        slice.forEach(([, { p, a }]) => { err += Math.abs(p - a); act += a; });
        return wapeAccuracy(err, act);
      };
      const early = halfAcc(dayEntries.slice(0, mid));
      const late = halfAcc(dayEntries.slice(mid));
      if (early != null && late != null) trendPP = late - early;
    }

    // Demand served = units where our forecast matched or exceeded actual need.
    // For each row: min(predicted, actual) — the portion we got right.
    let demandServed = 0;
    filteredRows.forEach((r) => {
      const p = toNum(r.predicted) ?? 0;
      const a = toNum(r.actual_qty) ?? 0;
      demandServed += Math.min(p, a);
    });
    const sellThroughPct = totalPred > 0 ? (Math.min(totalActual, totalPred) / totalPred) * 100 : null;

    return {
      accuracyPct: wapeAccuracy(scoredAbsErr, scoredActual),
      biasPct: totalActual > 0 ? ((totalPred - totalActual) / totalActual) * 100 : null,
      demandServed,
      sellThroughPct,
      totalActual,
      daysOnTarget,
      daysScored: dayEntries.length,
      bestStreak,
      bestDay,
      trendPP,
    };
  }, [filteredRows]);

  // Daily aggregated chart
  const dailyChart = useMemo(() => {
    const map = new Map<string, { predicted: number; actual: number }>();
    filteredRows.forEach((r) => {
      const d = String(r.trx_date ?? "").slice(0, 10);
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
  }, [filteredRows]);

  // Top 10 items by absolute variance -- bar chart
  const itemVarianceChart = useMemo(() => {
    const byItem = new Map<string, { predicted: number; actual: number }>();
    filteredRows.forEach((r) => {
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
  }, [filteredRows]);

  const windowLabel = `${params.start_date} to ${params.end_date}`;

  const biasAbs = stats.biasPct != null ? Math.abs(stats.biasPct) : null;
  const biasTrend =
    biasAbs == null ? undefined : biasAbs < BIAS_GOOD_PCT ? "up" : biasAbs < BIAS_WARN_PCT ? undefined : "down";

  // Smallest |variance| / actual across items -- our "most accurate item" win.
  // Only considers items whose 30-day actual volume crosses the same
  // significance threshold we use for lost-sales / dead-weight, so a SKU that
  // sold 1 unit doesn't hijack the highlight.
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
        detail: stats.bestDay.date,
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

  const trendValue =
    stats.trendPP == null
      ? "-"
      : stats.trendPP >= TREND_STEP_PP
      ? "Improving"
      : stats.trendPP <= -TREND_STEP_PP
      ? "Slipping"
      : "Steady";
  const trendSubtitle =
    stats.trendPP == null
      ? "Need more history"
      : `${Math.abs(stats.trendPP).toFixed(1)}% shift, last 15 days vs prior 15`;
  const trendArrow: "up" | "down" | undefined =
    stats.trendPP == null
      ? undefined
      : stats.trendPP >= TREND_STEP_PP
      ? "up"
      : stats.trendPP <= -TREND_STEP_PP
      ? "down"
      : undefined;

  return (
    <Drawer open={open} onClose={onClose} title="Last 30 Days Performance" width="xl">
      <div className="space-y-6">
        <DrawerContextBar
          routeCode={routeCode}
          itemCodes={itemCodes}
          dateRange={windowLabel}
          extra={
            filteredRows.length > 0 && (
              <span className="text-xs text-text-tertiary">
                {fmtNum(filteredRows.length)} scored rows
              </span>
            )
          }
        />

        {loading ? (
          <Loading message="Loading accuracy data..." />
        ) : data && data.success === false ? (
          <EmptyState
            icon="⚠️"
            title="Could not load accuracy data"
            message={data.error ?? "Backend returned an error."}
          />
        ) : filteredRows.length === 0 ? (
          <EmptyState
            title="No historical data"
            message={
              routeCode
                ? `No predictions matched actuals for route ${routeCode} in the last 30 days.`
                : "Pick a route to see accuracy."
            }
          />
        ) : (
          <>
            <KpiRow>
              <MetricCard
                label="Forecast accuracy"
                value={stats.accuracyPct != null ? `${stats.accuracyPct.toFixed(1)}%` : "-"}
                subtitle={
                  stats.daysScored > 0
                    ? `On target ${stats.daysOnTarget} of ${stats.daysScored} days`
                    : "No scored days"
                }
                trend={
                  stats.accuracyPct == null
                    ? undefined
                    : stats.accuracyPct >= GOOD_SCORE_THRESHOLD
                    ? "up"
                    : "down"
                }
              />
              <MetricCard
                label="On-target days"
                value={stats.daysScored > 0 ? `${stats.daysOnTarget} / ${stats.daysScored}` : "-"}
                subtitle={`Within ${Math.round(TOLERANCE_PCT * 100)}% of actual`}
                trend={
                  stats.daysScored === 0
                    ? undefined
                    : stats.daysOnTarget / stats.daysScored >= 0.7
                    ? "up"
                    : stats.daysOnTarget / stats.daysScored < 0.4
                    ? "down"
                    : undefined
                }
              />
              <MetricCard
                label="Van-load bias"
                value={
                  stats.biasPct != null
                    ? `${stats.biasPct > 0 ? "+" : ""}${stats.biasPct.toFixed(1)}%`
                    : "-"
                }
                subtitle="+ loaded too many, − too few"
                trend={biasTrend}
              />
              <MetricCard
                label="Accuracy trend"
                value={trendValue}
                subtitle={trendSubtitle}
                trend={trendArrow}
              />
            </KpiRow>

            <HighlightsStrip items={highlights} />

            <div>
              <p className="mb-2 text-caption uppercase tracking-wide text-text-tertiary">
                What our forecast delivered
              </p>
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                <MetricCard
                  label="Demand served"
                  value={fmtNum(stats.demandServed)}
                  subtitle="Units of customer demand our forecast correctly covered"
                  trend="up"
                />
                <MetricCard
                  label="Sell-through rate"
                  value={stats.sellThroughPct != null ? `${stats.sellThroughPct.toFixed(1)}%` : "-"}
                  subtitle="Share of recommended load customers actually bought"
                  trend={
                    stats.sellThroughPct != null && stats.sellThroughPct >= GOOD_SCORE_THRESHOLD
                      ? "up"
                      : undefined
                  }
                />
              </div>
            </div>

            <LineChart
              title="Recommended vs actual (daily)"
              data={dailyChart}
              xKey="date"
              series={[
                { key: "predicted", label: "Recommended" },
                { key: "actual", label: "Actual" },
              ]}
              height={300}
            />

            {itemVarianceChart.length > 0 && (
              <BarChart
                title="Items to fine-tune (largest forecast gap)"
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
