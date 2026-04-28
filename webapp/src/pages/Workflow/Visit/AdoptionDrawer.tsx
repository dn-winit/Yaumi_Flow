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
import DashboardFilterBar from "@/pages/Dashboard/DashboardFilterBar";

import { useAdoption } from "@/hooks/useRecommendedOrder";
import { useLookbackWindow } from "@/hooks/useDataImport";
import { fmtDate, fmtDateRange } from "@/lib/date";
import {
  fmtNum,
  fmtCurrency,
  DELIVERY_GOOD,
  DEFAULT_LOOKBACK,
  type Lookback,
} from "@/lib/format";
import type { DashboardFilters } from "@/types/data-import";
import { EMPTY_FILTERS } from "@/types/data-import";

interface Props {
  open: boolean;
  onClose: () => void;
  // The drawer is opened from a route's live session, so the route is
  // fixed in the filter scope. Warehouse + route are hidden in the
  // filter bar (redundant); the user can further narrow by category or
  // item via the same multi-select cascade the dashboard uses.
  routeCode?: string;
}

/**
 * Past-analysis drawer for recommendation adoption. Mirrors the Plan
 * step's Past-analysis drawer: same filter bar (Reporting period +
 * Category + Item, with Warehouse / Route hidden as redundant), same
 * lookback enum, same shared FilterDimensions hook.
 *
 * Backend `/analytics/adoption` honours category_codes + item_codes
 * directly so the metrics + charts here are real scoped views, not a
 * cosmetic filter bar over unfiltered data.
 */
export default function AdoptionDrawer({ open, onClose, routeCode }: Props) {
  const [lookback, setLookback] = useState<Lookback>(DEFAULT_LOOKBACK);
  const [filters, setFilters] = useState<DashboardFilters>(EMPTY_FILTERS);

  // Seed filter scope with the active route on every open. The user can
  // still widen via the bar; the route field is hidden in UI but pinned
  // in state so backend calls stay scoped.
  useEffect(() => {
    if (!open) return;
    setFilters({
      ...EMPTY_FILTERS,
      route_codes: routeCode ? [routeCode] : [],
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, routeCode]);

  // The reporting period maps to a *working-day* window served by
  // data_import (same dates the dashboard slices to for the same
  // lookback). One source of truth for "what counts as 30 working
  // days" -- this drawer never recomputes it from calendar deltas.
  const window = useLookbackWindow(open ? lookback : undefined);
  const w = window.data;
  const { params, windowLabel } = useMemo(() => {
    if (!w?.available || !w.start_date || !w.end_date) {
      return { params: null, windowLabel: "—" };
    }
    return {
      params: {
        start_date: w.start_date,
        end_date: w.end_date,
        ...(filters.route_codes.length === 1 ? { route_code: filters.route_codes[0] } : {}),
        ...(filters.category_codes.length > 0 ? { category_codes: filters.category_codes } : {}),
        ...(filters.item_codes.length > 0 ? { item_codes: filters.item_codes } : {}),
      },
      windowLabel: fmtDateRange(w.start_date, w.end_date),
    };
  }, [w, filters]);

  const { data, loading } = useAdoption(
    params ?? { start_date: "", end_date: "" },
    open && params != null,
  );
  const s = data?.summary ?? null;

  // All four tiles derive from the same backend summary snapshot.
  const derived = useMemo(() => {
    if (!s) return null;
    const pickAccuracyPct =
      s.skus_recommended > 0 ? (s.skus_adopted / s.skus_recommended) * 100 : null;
    const perfectPickPct =
      s.skus_adopted > 0 ? (s.skus_perfect / s.skus_adopted) * 100 : null;

    let bestDayDate = "";
    let bestDayPct = -1;
    (data?.daily ?? []).forEach((d) => {
      if (d.recommended > 0 && d.adoption_pct > bestDayPct) {
        bestDayPct = d.adoption_pct;
        bestDayDate = d.date;
      }
    });

    return {
      pickAccuracyPct,
      perfectPickPct,
      bestDay: bestDayPct >= 0 ? { date: bestDayDate, pct: bestDayPct } : null,
    };
  }, [s, data?.daily]);

  const highlights = useMemo<Highlight[]>(() => {
    if (!derived || !s) return [];
    const items: Highlight[] = [];
    if (derived.bestDay) {
      items.push({
        label: "Best day",
        value: `${derived.bestDay.pct.toFixed(1)}% hit rate`,
        detail: fmtDate(derived.bestDay.date),
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
  }, [derived, s]);

  const revenueArrow: "up" | "down" | undefined =
    s?.driven_revenue != null && s.driven_revenue > 0 ? "up" : undefined;
  const accuracyArrow: "up" | "down" | undefined =
    derived?.pickAccuracyPct == null
      ? undefined
      : derived.pickAccuracyPct >= DELIVERY_GOOD
      ? "up"
      : "down";
  const perfectPickArrow: "up" | "down" | undefined =
    derived?.perfectPickPct == null
      ? undefined
      : derived.perfectPickPct >= DELIVERY_GOOD
      ? "up"
      : "down";

  return (
    <Drawer open={open} onClose={onClose} title="Past analysis — recommendation follow-through" width="xl">
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
          <Loading message="Checking which recommendations sold..." />
        ) : !data?.available ? (
          <EmptyState
            icon="📭"
            title="Not enough history"
            message={
              data?.message ??
              `No recommendations were stored for ${windowLabel} matching the current filters.`
            }
          />
        ) : (
          <>
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
                    : `${fmtNum(s.driven_volume)} of ${fmtNum(s.recommended_volume)} suggested units sold · ${fmtNum(s.skus_adopted)} items`
                }
                trend={revenueArrow}
              />
              <MetricCard
                label="Items the customer took"
                value={
                  derived?.pickAccuracyPct != null
                    ? `${derived.pickAccuracyPct.toFixed(1)}%`
                    : "-"
                }
                subtitle={
                  s && s.skus_recommended > 0
                    ? `${fmtNum(s.skus_adopted)} of ${fmtNum(s.skus_recommended)} suggested items were bought`
                    : "No recommendations to score"
                }
                trend={accuracyArrow}
              />
              <MetricCard
                label="Right quantity, right item"
                value={
                  derived?.perfectPickPct != null
                    ? `${derived.perfectPickPct.toFixed(1)}%`
                    : "-"
                }
                subtitle={
                  s && s.skus_adopted > 0
                    ? `${fmtNum(s.skus_perfect)} of ${fmtNum(s.skus_adopted)} bought within ±${Math.round(s.perfect_pick_tolerance * 100)}% of suggested`
                    : "No adopted items to score"
                }
                trend={perfectPickArrow}
              />
              <MetricCard
                label="Suggested but didn't sell"
                value={
                  s?.unsold_revenue != null && s.unsold_revenue > 0
                    ? fmtCurrency(s.unsold_revenue)
                    : s?.unsold_volume
                    ? `${fmtNum(s.unsold_volume)} units`
                    : "0"
                }
                subtitle={
                  !s || s.unsold_volume === 0
                    ? "Every recommended unit sold"
                    : `${fmtNum(s.unsold_volume)} units · ${fmtNum(s.unsold_sku_count)} items the customer didn't take`
                }
              />
            </KpiRow>

            <HighlightsStrip items={highlights} />

            <div className="space-y-6">
              {data.daily.length > 0 && (
                <LineChart
                  title="Daily share of suggestions that sold"
                  data={data.daily as unknown as Record<string, unknown>[]}
                  xKey="date"
                  series={[{ key: "adoption_pct", label: "% sold", color: CHART_COLOR.success }]}
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
      </div>
    </Drawer>
  );
}
