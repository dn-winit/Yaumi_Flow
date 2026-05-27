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
import { fmtNum, hasRealConfidence, pickDate } from "@/lib/format";
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
  // Neutral copy; don't undermine trust in the displayed number.
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
  // Backend avg = total / active_days; labelled "/selling day" so avg×days==total verifies.
  return (
    <Stat
      label={label}
      value={
        <>
          {fmtNum(w.avg)}
          <span className="text-caption font-normal text-text-tertiary"> /selling day</span>
        </>
      }
      hint={`${w.active_days} selling days out of ${w.days} - total ${fmtNum(w.total)} units`}
    />
  );
}

export default function ExplainabilityModal({ open, onClose, row }: Props) {
  if (!row) return null;

  const itemCode = str(row.ItemCode ?? row.item_code);
  const itemName = str(row.ItemName ?? row.item_name);
  const routeCode = str(row.RouteCode ?? row.route_code);
  const date = pickDate(row);

  // Headline = total truck weight (carry + fresh). VanLoadTable adapter populates `prediction`.
  const recommendedLoad = num(row.prediction);
  const freshLoad = num(row.units_to_load) ?? recommendedLoad;
  const pDemand = num(row.p_demand);
  const q10 = num(row.lower_bound);
  const q90 = num(row.upper_bound);
  const cls = str(row.demand_class);
  // expectedDemand = engine's per-day target; recentAvg = per-selling-day baseline.
  const openingStock = num(row.opening_stock);
  const expectedDemand = num(row.expected_demand);
  const recentAvg = num(row.recent_avg_per_selling_day);
  const guardSkipped = row.guard_skipped === true;
  // Wire-driven flag; frontend never recomputes/thresholds.
  const forecastLow = bool(row.forecast_below_recent);
  // True only for two-stage classes (intermittent/lumpy); smooth/erratic render a static label.
  const hasRealProbability = hasRealConfidence(cls);
  // recentAvg populated -> cron has the recent pattern; absent -> brand-new item, drop the panel.
  const hasRecentAnchor = recentAvg != null && recentAvg > 0;

  const stats = useItemStats(open && itemCode ? itemCode : undefined, routeCode || undefined);
  const windows = stats.data?.windows;

  return (
    <Modal open={open} onClose={onClose} title="Why this recommendation" size="xl">
      <div className={MODAL_BODY}>
        <ExplainHeader
          left={{ label: "Item", primary: itemCode, secondary: itemName }}
          right={{ label: "Route / Date", primary: routeCode, secondary: fmtDate(date) }}
        />

        {/* Guard banner: journey-aware concentration mask zeroed this row (else 0 looks like a miss). */}
        {guardSkipped && (
          <div className="rounded-lg border border-warning-200 bg-warning-50 px-4 py-3 text-body text-warning-800">
            <strong>Skipped today:</strong> the customer who buys nearly all of this item isn&apos;t on
            today&apos;s journey plan, so the recommendation is held at zero. The truck rolls without
            phantom inventory.
          </div>
        )}

        {/* Section 1: recommendation -- headline, chance, range. */}
        <div>
          {/* Wire-driven low-forecast warning; uses the same tokens as guardSkipped. */}
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
              value={recommendedLoad != null ? fmtNum(recommendedLoad) : "-"}
              hint="Total units to put on the truck for this item (carried + fresh)"
              highlight
            />
            <Stat
              label="Chance of selling today"
              value={<ConfidenceBadge value={pDemand} demandClass={cls} />}
              // Honest hint only for two-stage classes; smooth/erratic get a different line.
              hint={
                hasRealProbability
                  ? "Probability at least one unit moves today"
                  : "This pattern sells too consistently for a per-day probability"
              }
            />
            <Stat
              label="Expected range"
              value={q10 != null && q90 != null ? `${fmtNum(q10)} - ${fmtNum(q90)}` : "-"}
              hint="Lower-to-upper band today's demand is calibrated to sit inside"
            />
          </div>
        </div>

        {/* Section 2: truck weight identity Carried + Fresh = Total; all server-stored, no math. */}
        {(openingStock != null || expectedDemand != null) && (
          <div>
            <SectionTitle>How we got the load</SectionTitle>

            {/* Three tiles so the user can verify the headline is the literal sum. */}
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <Stat
                label="Carried over"
                value={openingStock != null ? fmtNum(openingStock) : "-"}
                hint="Already on the truck from yesterday's leftover"
              />
              <Stat
                label="Fresh from depot"
                value={freshLoad != null ? fmtNum(freshLoad) : "-"}
                hint="What the depot issues today on top of the carry"
              />
              <Stat
                label="Total truck weight"
                value={recommendedLoad != null ? fmtNum(recommendedLoad) : "-"}
                hint="Carried + Fresh = today's van load"
                highlight
              />
            </div>

            {/* "Why this size" pair; drops out gracefully when the item is brand new. */}
            {(expectedDemand != null || hasRecentAnchor) && (
              <div className="mt-3 grid grid-cols-1 sm:grid-cols-2 gap-3">
                <Stat
                  label="Expected today"
                  value={expectedDemand != null ? fmtNum(expectedDemand) : "-"}
                  hint="How many units we expect to sell on this route today"
                />
                <Stat
                  label="Recent pattern"
                  value={hasRecentAnchor ? `${fmtNum(recentAvg!)} /selling day` : "-"}
                  hint={
                    hasRecentAnchor
                      ? "Average units sold on the days this item moved"
                      : "No recent selling history for this item yet"
                  }
                />
              </div>
            )}
          </div>
        )}

        {/* Section 3: rolling-window demand history at a glance. */}
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
