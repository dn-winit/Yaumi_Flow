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
import { useItemPrices } from "@/hooks/useDataImport";
import {
  fmtNum,
  fmtCurrency,
  toNum,
  GOOD_SCORE_THRESHOLD,
  TOLERANCE_PCT,
  LEAKAGE_SHARE_WARN,
  ON_TARGET_GOOD_RATIO,
  ON_TARGET_POOR_RATIO,
  LAST_30_DAYS,
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
  const { params, windowDays } = useMemo(() => {
    const endDate = todayIso();
    const startDate = addDays(endDate, -(LAST_30_DAYS - 1));
    const p: Record<string, unknown> = {
      start_date: startDate,
      end_date: endDate,
    };
    if (routeCode) p.route_code = routeCode;
    // Only push item_code to the API when a single SKU is picked. With 2+ SKUs
    // the API can only filter one, so we fetch the route-wide window and filter
    // client-side below; the backend summary in that case isn't useful.
    if (itemCodes && itemCodes.length === 1) p.item_code = itemCodes[0];
    return { params: p, windowDays: LAST_30_DAYS };
  }, [routeCode, itemCodes]);

  // Only run the cross-DB join when the drawer is actually open.
  const { data, loading } = useAccuracyComparison(params, open);
  const prices = useItemPrices(open);

  const multiItemFilter = !!(itemCodes && itemCodes.length > 1);

  const filteredRows = useMemo(() => {
    const rows = (data?.rows ?? []) as unknown as Row[];
    if (!multiItemFilter) return rows;
    const set = new Set(itemCodes);
    return rows.filter((r) => set.has(String(r.item_code ?? "")));
  }, [data, itemCodes, multiItemFilter]);

  // Single-pass client-side stats.
  //
  // Four tiles — arithmetic identity on the FORECAST dimension:
  //   demandServed + unsoldForecast = totalPredicted (every forecasted unit
  //   either sold through or stayed in the van). Tile 1 and Tile 4 reconcile
  //   exactly on what we told the van to carry.
  //
  //   * demandServed    -- Σ min(predicted, actual): forecast units that sold
  //   * accuracyPct     -- WAPE-based quality; scored where both > 0
  //   * daysOnTarget    -- days with |pred − actual|/actual ≤ TOLERANCE_PCT
  //   * Lost sales      -- Σ max(0, predicted − actual): forecast units that
  //                        DIDN'T sell -- stock we loaded, customers didn't take,
  //                        valued at avg unit price
  //
  // Highlights strip reuses bestDay + bestStreak from the same pass.
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

    filteredRows.forEach((r) => {
      const p = toNum(r.predicted) ?? 0;
      const a = toNum(r.actual_qty) ?? 0;
      totalActual += a;
      totalPredicted += p;
      // Score rows where actual > 0 AND predicted > 0 -- same filter the
      // backend wape_summary helper uses so every accuracy display agrees.
      if (a > 0 && p > 0) {
        scoredAbsErr += Math.abs(a - p);
        scoredActual += a;
      }
      // Decompose forecast into sold-through + unsold.
      demandServed += Math.min(p, a);
      const excess = Math.max(0, p - a);
      if (excess > 0) {
        unsoldForecast += excess;
        const code = String(r.item_code ?? "");
        const price = code ? prices[code] ?? 0 : 0;
        if (price > 0) {
          unsoldRevenue += excess * price;
          pricesSeen = true;
        }
      }

      const d = String(r.trx_date ?? "").slice(0, 10);
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

    // SKU counts for the context bar + tile subtitles:
    //   forecastedSkuCount -- master set: items our forecast told the van to carry
    //   servedSkuCount     -- subset: forecast items that sold at least some units
    //   unsoldSkuCount     -- subset: items where forecast exceeded actual
    // A partially-over-forecast SKU can appear in served AND unsold; that's
    // honest since it contributed units to served AND to the unsold remainder.
    let forecastedSkuCount = 0;
    let servedSkuCount = 0;
    let unsoldSkuCount = 0;
    byItem.forEach(({ predicted, actual }) => {
      if (predicted > 0) forecastedSkuCount += 1;
      if (predicted > 0 && actual > 0) servedSkuCount += 1;
      if (predicted > actual) unsoldSkuCount += 1;
    });

    // Day entries for on-target count, streak + best-day highlights.
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
  }, [filteredRows, prices]);

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

  // Arrows only on tiles where "up = unambiguously good." Lost sales tile
  // carries no arrow -- it's a loss magnitude, not a direction.
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

  return (
    <Drawer open={open} onClose={onClose} title="Last 30 Days Performance" width="xl">
      <div className="space-y-6">
        <DrawerContextBar
          routeCode={routeCode}
          itemCodes={itemCodes}
          dateRange={windowLabel}
          extra={
            stats.forecastedSkuCount > 0 && (
              <span className="text-caption text-text-tertiary">
                {fmtNum(stats.forecastedSkuCount)} unique items forecast
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
                    ? `${stats.daysScored} of ${windowDays} days with records`
                    : `No records in last ${windowDays} days`
                }
                trend={accuracyArrow}
              />
              <MetricCard
                label="On-target days"
                value={stats.daysScored > 0 ? `${stats.daysOnTarget} / ${stats.daysScored}` : "-"}
                subtitle={`Days within ${Math.round(TOLERANCE_PCT * 100)}% of actual`}
                trend={onTargetArrow}
              />
              <MetricCard
                label="Lost sales"
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
                    : `${fmtNum(stats.unsoldForecast)} units · ${fmtNum(stats.unsoldSkuCount)} items we forecast that didn't sell`
                }
              />
            </KpiRow>

            <HighlightsStrip items={highlights} />

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
