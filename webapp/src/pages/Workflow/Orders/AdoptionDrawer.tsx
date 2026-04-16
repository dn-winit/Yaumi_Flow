import { useMemo } from "react";
import Drawer from "@/components/ui/Drawer";
import Loading from "@/components/ui/Loading";
import EmptyState from "@/components/ui/EmptyState";
import Card from "@/components/ui/Card";
import Badge from "@/components/ui/Badge";
import DrawerContextBar from "@/components/ui/DrawerContextBar";
import KpiRow from "@/components/ui/KpiRow";
import HighlightsStrip, { type Highlight } from "@/components/ui/HighlightsStrip";
import MetricCard from "@/components/charts/MetricCard";
import LineChart from "@/components/charts/LineChart";
import BarChart from "@/components/charts/BarChart";
import { CHART_COLOR } from "@/components/charts/theme";

import { useAdoption } from "@/hooks/useRecommendedOrder";
import { addDays, todayIso } from "@/lib/date";
import {
  fmtNum,
  HIT_RATE_GOOD,
  COVERAGE_GOOD,
  TREND_STEP_PP,
} from "@/lib/format";

interface Props {
  open: boolean;
  onClose: () => void;
  routeCode?: string;
}

export default function AdoptionDrawer({ open, onClose, routeCode }: Props) {
  const end_date = todayIso();
  const start_date = addDays(end_date, -29);

  const params = { start_date, end_date, ...(routeCode ? { route_code: routeCode } : {}) };
  const { data, loading } = useAdoption(params, open);

  const windowLabel = `${start_date} to ${end_date}`;
  const s = data?.summary ?? null;

  // Derived stats + highlights: single memo so the four tiles and the strip
  // read from the same snapshot of `data`.
  const derived = useMemo(() => {
    if (!s) return null;

    // Coverage: of every item customers bought on recommended days, what
    // fraction was on our list? adopted + missed == total-bought-items in the
    // window for scored rows, so this is a clean precision complement.
    const coverageDenom = s.rows_adopted + s.rows_missed;
    const coveragePct = coverageDenom > 0 ? (s.rows_adopted / coverageDenom) * 100 : null;

    // Trend: late-half avg adoption_pct minus early-half. Needs >=2 scored
    // days (days with at least one recommendation) to be meaningful.
    const daily = (data?.daily ?? []).filter((d) => d.recommended > 0);
    let trendPP: number | null = null;
    if (daily.length >= 2) {
      const mid = Math.floor(daily.length / 2);
      const avg = (slice: typeof daily) =>
        slice.reduce((n, d) => n + (d.adoption_pct ?? 0), 0) / slice.length;
      trendPP = avg(daily.slice(mid)) - avg(daily.slice(0, mid));
    }

    // Highlights -- positive framing pulled from the same dataset.
    let bestDayDate = "";
    let bestDayPct = -1;
    daily.forEach((d) => {
      if (d.adoption_pct > bestDayPct) {
        bestDayPct = d.adoption_pct;
        bestDayDate = d.date;
      }
    });

    let bestTier = "";
    let bestTierPct = -1;
    let bestTierAdopted = 0;
    let bestTierRecommended = 0;
    (data?.by_tier ?? []).forEach((t) => {
      if (t.recommended <= 0) return;
      if (t.adoption_pct > bestTierPct) {
        bestTierPct = t.adoption_pct;
        bestTier = t.tier;
        bestTierAdopted = t.adopted;
        bestTierRecommended = t.recommended;
      }
    });

    return {
      coveragePct,
      trendPP,
      bestDay: bestDayPct >= 0 ? { date: bestDayDate, pct: bestDayPct } : null,
      bestTier: bestTierPct >= 0
        ? { tier: bestTier, pct: bestTierPct, adopted: bestTierAdopted, recommended: bestTierRecommended }
        : null,
    };
  }, [s, data?.daily, data?.by_tier]);

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
    if (derived.bestTier) {
      items.push({
        label: "Strongest band",
        value: derived.bestTier.tier,
        detail: `${derived.bestTier.pct.toFixed(1)}% · ${fmtNum(derived.bestTier.adopted)} of ${fmtNum(derived.bestTier.recommended)} sold`,
      });
    }
    if (s.rows_adopted > 0) {
      items.push({
        label: "Sales captured",
        value: `${fmtNum(s.rows_adopted)} items`,
        detail: "Recommended and bought in the window",
      });
    }
    return items;
  }, [derived, s]);

  const trendValue =
    !derived || derived.trendPP == null
      ? "-"
      : derived.trendPP >= TREND_STEP_PP
      ? "Improving"
      : derived.trendPP <= -TREND_STEP_PP
      ? "Slipping"
      : "Steady";
  const trendSubtitle =
    !derived || derived.trendPP == null
      ? "Need more history"
      : `${Math.abs(derived.trendPP).toFixed(1)}% shift, last 15 days vs prior 15`;
  const trendArrow: "up" | "down" | undefined =
    !derived || derived.trendPP == null
      ? undefined
      : derived.trendPP >= TREND_STEP_PP
      ? "up"
      : derived.trendPP <= -TREND_STEP_PP
      ? "down"
      : undefined;

  const hitArrow: "up" | "down" | undefined =
    s?.adoption_pct == null
      ? undefined
      : s.adoption_pct >= HIT_RATE_GOOD
      ? "up"
      : "down";
  const coverageArrow: "up" | "down" | undefined =
    derived?.coveragePct == null
      ? undefined
      : derived.coveragePct >= COVERAGE_GOOD
      ? "up"
      : "down";
  const liftArrow: "up" | "down" | undefined =
    s?.uplift_pct == null ? undefined : s.uplift_pct > 0 ? "up" : "down";

  return (
    <Drawer open={open} onClose={onClose} title="Last 30 days - recommendation follow-through" width="xl">
      <div className="space-y-6">
        <DrawerContextBar
          routeCode={routeCode}
          dateRange={windowLabel}
          extra={
            s ? (
              <span className="text-xs text-text-tertiary">
                {fmtNum(s.rows_recommended)} recommendations reviewed
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
                label="Hit rate"
                value={s?.adoption_pct != null ? `${s.adoption_pct.toFixed(1)}%` : "-"}
                subtitle={`${fmtNum(s?.rows_adopted)} of ${fmtNum(s?.rows_recommended)} recommendations sold`}
                trend={hitArrow}
              />
              <MetricCard
                label="Coverage"
                value={derived?.coveragePct != null ? `${derived.coveragePct.toFixed(1)}%` : "-"}
                subtitle={
                  derived?.coveragePct != null && s
                    ? `${fmtNum(s.rows_adopted)} of ${fmtNum(s.rows_adopted + s.rows_missed)} customer buys on our list`
                    : "Of items customers bought, share on our list"
                }
                trend={coverageArrow}
              />
              <MetricCard
                label="Sales lift"
                value={
                  s?.uplift_pct != null
                    ? `${s.uplift_pct > 0 ? "+" : ""}${s.uplift_pct.toFixed(1)}%`
                    : "-"
                }
                subtitle="Extra units sold when recommended vs when not"
                trend={liftArrow}
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

                {data.by_tier.length > 0 && (
                  <Card title="Follow-through by recommendation band">
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                      {data.by_tier.map((t) => (
                        <div
                          key={t.tier}
                          className="bg-surface-sunken rounded-lg px-3 py-2.5 border border-subtle"
                        >
                          <div className="flex items-center justify-between">
                            <Badge variant="info">{t.tier}</Badge>
                            <span className="text-sm font-semibold text-text-primary">
                              {t.adoption_pct.toFixed(1)}%
                            </span>
                          </div>
                          <div className="mt-1 text-[11px] text-text-tertiary">
                            {fmtNum(t.adopted)} of {fmtNum(t.recommended)} sold
                          </div>
                        </div>
                      ))}
                    </div>
                  </Card>
                )}
              </div>
            </div>
          </>
        )}
      </div>
    </Drawer>
  );
}
