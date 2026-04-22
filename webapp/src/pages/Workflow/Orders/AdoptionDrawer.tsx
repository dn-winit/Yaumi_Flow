import { useMemo } from "react";
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

import { useAdoption } from "@/hooks/useRecommendedOrder";
import { addDays, todayIso } from "@/lib/date";
import { fmtNum, fmtCurrency, DELIVERY_GOOD } from "@/lib/format";

interface Props {
  open: boolean;
  onClose: () => void;
  routeCode?: string;
}

export default function AdoptionDrawer({ open, onClose, routeCode }: Props) {
  const { params, windowLabel } = useMemo(() => {
    const endDate = todayIso();
    const days = 30;
    const startDate = addDays(endDate, -(days - 1));
    return {
      params: {
        start_date: startDate,
        end_date: endDate,
        ...(routeCode ? { route_code: routeCode } : {}),
      },
      windowLabel: `${startDate} to ${endDate}`,
    };
  }, [routeCode]);
  const { data, loading } = useAdoption(params, open);
  const s = data?.summary ?? null;

  // All four tiles derive from the same backend summary snapshot.
  // Arrows are computed once so the JSX stays declarative.
  const derived = useMemo(() => {
    if (!s) return null;

    // Tile 2: pick accuracy (unique-SKU hit rate)
    const pickAccuracyPct =
      s.skus_recommended > 0 ? (s.skus_adopted / s.skus_recommended) * 100 : null;

    // Tile 3: share of adopted SKUs whose quantity landed inside the backend
    // tolerance band. Denominator = adopted SKUs (Tile 2 numerator).
    const perfectPickPct =
      s.skus_adopted > 0 ? (s.skus_perfect / s.skus_adopted) * 100 : null;

    // Best day highlight reads from the daily breakdown (row-grain) -- still
    // meaningful for trend spotting even though the tiles are SKU-grain.
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
        detail: derived.bestDay.date,
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

  // Arrows only on tiles where "up = unambiguously good." Lost sales
  // carries no arrow -- it's a loss magnitude, not a direction.
  const revenueArrow: "up" | "down" | undefined =
    s?.actual_revenue != null && s.actual_revenue > 0 ? "up" : undefined;
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
    <Drawer open={open} onClose={onClose} title="Last 30 days - recommendation follow-through" width="xl">
      <div className="space-y-6">
        <DrawerContextBar
          routeCode={routeCode}
          dateRange={windowLabel}
          extra={
            s ? (
              <span className="text-caption text-text-tertiary">
                {fmtNum(s.skus_recommended)} unique items recommended
              </span>
            ) : null
          }
        />

        {loading ? (
          <Loading message="Checking which recommendations sold..." />
        ) : !data?.available ? (
          <EmptyState
            icon="📭"
            title="Not enough history"
            message={data?.message ?? "No recommendations were stored for this window."}
          />
        ) : (
          <>
            <KpiRow>
              <MetricCard
                label="Revenue driven by our list"
                value={
                  s?.actual_revenue != null && s.actual_revenue > 0
                    ? fmtCurrency(s.actual_revenue)
                    : s?.actual_volume
                    ? `${fmtNum(s.actual_volume)} units`
                    : "-"
                }
                subtitle={
                  s && s.actual_volume > 0
                    ? `${fmtNum(s.actual_volume)} units sold across ${fmtNum(s.skus_adopted)} items`
                    : "No recommendations converted yet"
                }
                trend={revenueArrow}
              />
              <MetricCard
                label="Pick accuracy"
                value={
                  derived?.pickAccuracyPct != null
                    ? `${derived.pickAccuracyPct.toFixed(1)}%`
                    : "-"
                }
                subtitle={
                  s && s.skus_recommended > 0
                    ? `${fmtNum(s.skus_adopted)} of ${fmtNum(s.skus_recommended)} recommended items sold`
                    : "No recommendations to score"
                }
                trend={accuracyArrow}
              />
              <MetricCard
                label="Perfect picks"
                value={
                  derived?.perfectPickPct != null
                    ? `${derived.perfectPickPct.toFixed(1)}%`
                    : "-"
                }
                subtitle={
                  s && s.skus_adopted > 0
                    ? `${fmtNum(s.skus_perfect)} of ${fmtNum(s.skus_adopted)} adopted items within ±${Math.round(s.perfect_pick_tolerance * 100)}% of actual`
                    : "No adopted items to score"
                }
                trend={perfectPickArrow}
              />
              <MetricCard
                label="Lost sales"
                value={
                  s?.lost_revenue != null && s.lost_revenue > 0
                    ? fmtCurrency(s.lost_revenue)
                    : s?.lost_sales_units
                    ? `${fmtNum(s.lost_sales_units)} units`
                    : "0"
                }
                subtitle={
                  !s?.skus_over_recommended
                    ? "Every recommended item sold"
                    : `${fmtNum(s.skus_over_recommended)} items · ${fmtNum(s.lost_sales_units)} units never sold`
                }
              />
            </KpiRow>

            <HighlightsStrip items={highlights} />

            <div>
              <p className="mb-2 text-caption uppercase tracking-wide text-text-tertiary">
                Performance over time
              </p>
              <div className="space-y-6">
                {data.daily.length > 0 && (
                  <LineChart
                    title="Daily hit rate"
                    data={data.daily as unknown as Record<string, unknown>[]}
                    xKey="date"
                    series={[{ key: "adoption_pct", label: "Hit rate %", color: CHART_COLOR.success }]}
                    height={260}
                  />
                )}

                <p className="text-caption uppercase tracking-wide text-text-tertiary">
                  Next-cycle tuning
                </p>
                <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
                  {data.top_over_recommended.length > 0 && (
                    <BarChart
                      title="Items to right-size (van space we can reallocate)"
                      data={data.top_over_recommended as unknown as Record<string, unknown>[]}
                      xKey="item_code"
                      yKey="rows"
                      color={CHART_COLOR.warning}
                      height={240}
                    />
                  )}
                  {data.top_missed.length > 0 && (
                    <BarChart
                      title="New demand spotted (items customers are buying)"
                      data={data.top_missed as unknown as Record<string, unknown>[]}
                      xKey="item_code"
                      yKey="rows"
                      color={CHART_COLOR.success}
                      height={240}
                    />
                  )}
                </div>
              </div>
            </div>
          </>
        )}
      </div>
    </Drawer>
  );
}
