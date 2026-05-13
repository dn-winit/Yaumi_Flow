import Modal from "./Modal";
import Badge from "./Badge";
import ConfidenceBadge from "./ConfidenceBadge";
import {
  ExplainHeader,
  GRID_3,
  MODAL_BODY,
  SectionTitle,
  Stat,
  bool,
  num,
  str,
} from "./explain/atoms";
import { pickDate } from "@/lib/format";
import { fmtDate } from "@/lib/date";
import { useItemStats } from "@/hooks/useDataImport";
import type { ItemStatsWindow } from "@/types/data-import";
import type { Row } from "@/types/common";

interface Props {
  open: boolean;
  onClose: () => void;
  row: Row | null;
}

function classDesc(cls: string): string {
  // Neutral, factual descriptions -- avoid words like "harder to predict"
  // that undermine the supervisor's trust in the number on screen.
  const c = cls.toLowerCase();
  if (c === "smooth") return "Sells most days in steady quantities";
  if (c === "intermittent") return "Sells in bursts, fairly steady sizes";
  if (c === "erratic") return "Sells most days, quantities vary";
  if (c === "lumpy") return "Sells in bursts, quantities vary";
  return "";
}

function patternTone(cls: string): "info" | "warning" | "danger" | "neutral" {
  const c = cls.toLowerCase();
  if (c === "smooth") return "info";
  if (c === "intermittent") return "warning";
  if (c === "erratic") return "danger";
  return "neutral";
}

function WindowStat({ label, w }: { label: string; w: ItemStatsWindow | null | undefined }) {
  if (!w || w.avg == null) {
    return <Stat label={label} value="-" hint="No demand in this window" />;
  }
  // Backend computes avg as total / active_days, so the displayed figure is
  // the typical quantity on days the item actually sold -- not a calendar-day
  // average. Labelling it "/selling day" keeps the math honest: the user can
  // verify avg x active_days == total from the hint.
  return (
    <Stat
      label={label}
      value={
        <>
          {w.avg.toFixed(1)}
          <span className="text-caption font-normal text-text-tertiary"> /selling day</span>
        </>
      }
      hint={`${w.active_days} selling days out of ${w.days} - total ${w.total.toFixed(0)} units`}
    />
  );
}

export default function ExplainabilityModal({ open, onClose, row }: Props) {
  if (!row) return null;

  const itemCode = str(row.ItemCode ?? row.item_code);
  const itemName = str(row.ItemName ?? row.item_name);
  const routeCode = str(row.RouteCode ?? row.route_code);
  const date = pickDate(row);

  // ``prediction`` / ``predicted`` carry the reconciled van load
  // (units_to_load) -- the cron's recommended_load + opening_stock for
  // this (route, item, date). Same number the headline tile shows.
  const recommendedLoad = num(row.prediction ?? row.predicted);
  const pDemand = num(row.p_demand);
  // Canonical column names from the DB-mirror are
  // ``lower_bound``/``upper_bound`` and ``demand_class``. Older payloads
  // used ``q_10``/``q_90`` and ``class``; try the canonical names first.
  const q10 = num(row.lower_bound ?? row.q_10);
  const q90 = num(row.upper_bound ?? row.q_90);
  const cls = str(row.demand_class ?? row.class);
  // Engine intermediates -- the "show your work" math.
  const openingStock = num(row.opening_stock);
  const forecastCorrected = num(row.forecast_corrected);
  const biasPct = num(row.bias_pct);
  const predictedRaw = num(row.predicted_raw);
  // Pattern-envelope reconciliation step. `recentAvg` is the per-selling-day
  // baseline the envelope is measured against; `expectedDemand` is the
  // post-envelope number the engine actually consumes. The two booleans
  // indicate whether the envelope pulled the forecast UP (floor) or DOWN
  // (ceiling) -- they are mutually exclusive on a given row.
  const recentAvg = num(row.recent_avg_per_selling_day);
  const expectedDemand = num(row.expected_demand);
  const patternFloorApplied = bool(row.pattern_floor_applied);
  const patternCeilingApplied = bool(row.pattern_ceiling_applied);
  const guardSkipped = row.guard_skipped === true;
  // Wire-driven informational flag: backend sets this when the corrected
  // forecast significantly under-shoots the item's recent activity. The
  // frontend NEVER recomputes or thresholds -- it just renders the chip
  // when the server says so. Falsy/missing -> chip hidden.
  const forecastLow = bool(row.forecast_below_recent);

  const stats = useItemStats(open && itemCode ? itemCode : undefined, routeCode || undefined);
  const windows = stats.data?.windows;

  return (
    <Modal open={open} onClose={onClose} title="Why this recommendation" size="xl">
      <div className={MODAL_BODY}>
        <ExplainHeader
          left={{ label: "Item", primary: itemCode, secondary: itemName }}
          right={{ label: "Route / Date", primary: routeCode, secondary: fmtDate(date) }}
        />

        {/* Guard banner: appears only when the journey-aware
            concentration mask zeroed this row. Without it, a "0" load
            with no explanation looks like a model miss. */}
        {guardSkipped && (
          <div className="rounded-lg border border-warning-200 bg-warning-50 px-4 py-3 text-body text-warning-800">
            <strong>Skipped today:</strong> the customer who buys nearly all of this item isn&apos;t on
            today&apos;s journey plan, so the recommendation is held at zero. The truck rolls without
            phantom inventory.
          </div>
        )}

        {/* Section 1: the recommendation itself. The headline number,
            the chance it moves at all, the expected band. */}
        <div>
          {/* Wire-driven low-forecast warning. Renders only when the
              backend flags this (route, item, date) row. Matches the
              guardSkipped banner tokens so the two feel of-a-piece. */}
          {forecastLow && (
            <div className="mb-2 rounded-md border border-warning-200 bg-warning-50 px-3 py-2 text-body text-warning-800">
              Forecast looks low for this item&apos;s recent pattern - worth reviewing
            </div>
          )}
          <SectionTitle>
            Recommendation
            {cls && (
              <Badge tone={patternTone(cls)} className="ml-2 text-caption">
                {cls} - {classDesc(cls)}
              </Badge>
            )}
          </SectionTitle>
          <div className={GRID_3}>
            <Stat
              label="Recommended van load"
              value={recommendedLoad != null ? Math.round(recommendedLoad).toLocaleString() : "-"}
              hint="Total units to put on the truck for this item (carried + fresh)"
              highlight
            />
            <Stat
              label="Chance of selling today"
              value={<ConfidenceBadge value={pDemand} demandClass={cls} />}
              hint="Probability at least one unit moves today"
            />
            <Stat
              label="Expected range"
              value={
                q10 != null && q90 != null
                  ? `${Math.round(q10).toLocaleString()} - ${Math.round(q90).toLocaleString()}`
                  : "-"
              }
              hint="Lower-to-upper band today's demand is calibrated to sit inside"
            />
          </div>
        </div>

        {/* Section 2: how the truck weight breaks down. Only renders
            when at least one engine intermediate is populated, so legacy
            rows without the diagnostics stay clean. */}
        {(openingStock != null || forecastCorrected != null || biasPct != null || predictedRaw != null || recentAvg != null) && (
          <div>
            <SectionTitle>How we got the load</SectionTitle>
            <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
              <Stat
                label="Raw forecast"
                value={
                  predictedRaw != null
                    ? Math.round(predictedRaw).toLocaleString()
                    : "-"
                }
                hint="Model output before any adjustment"
              />
              <Stat
                label="Model trend"
                value={
                  biasPct != null
                    ? `${biasPct > 0 ? "+" : ""}${(biasPct * 100).toFixed(1)}%`
                    : "-"
                }
                hint="How much we adjust: positive trims down, negative boosts up"
              />
              <Stat
                label="Adjusted forecast"
                value={
                  forecastCorrected != null
                    ? Math.round(forecastCorrected).toLocaleString()
                    : "-"
                }
                hint="Raw forecast x (1 - trend) = adjusted"
              />
              <Stat
                label="Recent average"
                value={
                  recentAvg != null
                    ? Math.round(recentAvg).toLocaleString()
                    : "-"
                }
                hint="Item's typical units per selling day (last 28 working days)"
              />
              <Stat
                label="Already on truck"
                value={
                  openingStock != null
                    ? Math.round(openingStock).toLocaleString()
                    : "-"
                }
                hint="Carried from yesterday - no fresh load needed for these units"
              />
            </div>
            {patternFloorApplied && expectedDemand != null && (
              <p className="mt-2 text-caption text-info-700">
                Pattern floor applied: expected demand boosted to {Math.round(expectedDemand).toLocaleString()} to stay within the
                item&apos;s recent pattern.
              </p>
            )}
            {patternCeilingApplied && !patternFloorApplied && expectedDemand != null && (
              <p className="mt-2 text-caption text-info-700">
                Pattern ceiling applied: expected demand capped at {Math.round(expectedDemand).toLocaleString()} based on the
                item&apos;s recent pattern.
              </p>
            )}
          </div>
        )}

        {/* Section 3: demand history (rolling windows). Three windows
            on one row so the supervisor sees the item's recent activity
            at a glance -- the anchor for why today's recommendation is
            what it is. */}
        <div>
          <SectionTitle right={stats.loading ? "loading..." : undefined}>
            Demand history
          </SectionTitle>
          {stats.data?.available === false ? (
            <div className="text-body text-text-tertiary bg-surface-sunken rounded-lg px-3 py-2 border border-subtle">
              {stats.data.message ?? "No historical sales for this item"}
            </div>
          ) : (
            <div className={GRID_3}>
              <WindowStat label="Last week" w={windows?.last_week} />
              <WindowStat label="Last 4 weeks" w={windows?.last_4_weeks} />
              <WindowStat label="Last 3 months" w={windows?.last_3_months} />
            </div>
          )}
        </div>
      </div>
    </Modal>
  );
}
