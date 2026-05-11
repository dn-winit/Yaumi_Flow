import Modal from "./Modal";
import Badge from "./Badge";
import ConfidenceBadge from "./ConfidenceBadge";
import {
  ExplainHeader,
  GRID_3,
  MODAL_BODY,
  SectionTitle,
  Stat,
  num,
  str,
} from "./explain/atoms";
import { pickDate } from "@/lib/format";
import { fmtDate } from "@/lib/date";
import { accuracyTone } from "@/lib/colorize";
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
  // verify avg × active_days ≈ total from the hint.
  return (
    <Stat
      label={label}
      value={
        <>
          {w.avg.toFixed(1)}
          <span className="text-caption font-normal text-text-tertiary"> /selling day</span>
        </>
      }
      hint={`${w.active_days} selling days out of ${w.days} · total ${w.total.toFixed(0)} units`}
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
  const actual = num(row.actual_qty ?? row.TotalQuantity);
  const pDemand = num(row.p_demand);
  const qtyIfDemand = num(row.qty_if_demand);
  // Canonical column names from the DB-mirror are
  // ``lower_bound``/``upper_bound`` and ``demand_class``. Older payloads
  // used ``q_10``/``q_90`` and ``class``; try the canonical names first.
  const q10 = num(row.lower_bound ?? row.q_10);
  const q90 = num(row.upper_bound ?? row.q_90);
  const cls = str(row.demand_class ?? row.class);
  const nonzeroRatio = num(row.nonzero_ratio);
  const avgGapDays = num(row.avg_gap_days);
  // Engine intermediates -- the "show your work" math.
  const openingStock = num(row.opening_stock);
  const forecastCorrected = num(row.forecast_corrected);
  const biasPct = num(row.bias_pct);
  const guardSkipped = row.guard_skipped === true;

  const stats = useItemStats(open && itemCode ? itemCode : undefined, routeCode || undefined);
  const windows = stats.data?.windows;

  // Load vs sold gap (NOT model accuracy -- this compares the truck
  // weight against actual invoiced units; raw forecast accuracy lives
  // on the Pipeline page).
  const variance = recommendedLoad != null && actual != null ? actual - recommendedLoad : null;
  const variancePct =
    variance != null && recommendedLoad && recommendedLoad > 0
      ? (variance / recommendedLoad) * 100
      : null;

  return (
    <Modal open={open} onClose={onClose} title="Why this forecast" size="xl">
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

        {/* Section 1: The recommendation itself */}
        <div>
          <SectionTitle>
            Recommendation
            {cls && (
              <Badge tone={patternTone(cls)} className="ml-2 text-caption">
                {cls} — {classDesc(cls)}
              </Badge>
            )}
          </SectionTitle>
          <div className={GRID_3}>
            <Stat
              label="Recommended van load"
              value={recommendedLoad != null ? Math.round(recommendedLoad).toLocaleString() : "-"}
              hint="Units to put on the truck for this item — fresh + carried"
              highlight
            />
            <Stat
              label="Likely to sell today"
              value={<ConfidenceBadge value={pDemand} demandClass={cls} />}
              hint="Probability the item moves at least one unit today"
            />
            <Stat
              label="Likely range"
              value={
                q10 != null && q90 != null
                  ? `${Math.round(q10).toLocaleString()} – ${Math.round(q90).toLocaleString()}`
                  : "-"
              }
              hint="Conformal lower-to-upper band the load is calibrated to sit inside"
            />
          </div>
        </div>

        {/* Section 1b: How the truck weight breaks down. Backend ships
            opening_stock / forecast_corrected / bias_pct in every
            payload; renders only when they're populated so legacy rows
            without the engine intermediates stay clean. */}
        {(openingStock != null || forecastCorrected != null || biasPct != null) && (
          <div>
            <SectionTitle>How we got the load</SectionTitle>
            <div className={GRID_3}>
              <Stat
                label="Bias-corrected forecast"
                value={
                  forecastCorrected != null
                    ? Math.round(forecastCorrected).toLocaleString()
                    : "-"
                }
                hint="Raw model output trimmed by the route+item bias"
              />
              <Stat
                label="Stock on van"
                value={
                  openingStock != null
                    ? Math.round(openingStock).toLocaleString()
                    : "-"
                }
                hint="Units already on the truck — leftover from yesterday"
              />
              <Stat
                label="Recent over/under"
                value={
                  biasPct != null
                    ? `${biasPct > 0 ? "+" : ""}${(biasPct * 100).toFixed(1)}%`
                    : "-"
                }
                hint="Model has been over (+) or under (−) actuals for this route+item"
              />
            </div>
          </div>
        )}

        {/* Section 2: Anchor the recommendation in the item's own pattern.
            Renders only when at least one of the contextual stats exists,
            so the popup stays clean for legacy rows that lack them. */}
        {(qtyIfDemand != null || nonzeroRatio != null || avgGapDays != null) && (
          <div>
            <SectionTitle>Item demand pattern</SectionTitle>
            <div className={GRID_3}>
              <Stat
                label="Expected qty when it sells"
                value={qtyIfDemand != null ? Math.round(qtyIfDemand).toLocaleString() : "-"}
                hint="Average size of a buying day for this item"
              />
              <Stat
                label="Sells how often"
                value={
                  nonzeroRatio != null
                    ? `${(nonzeroRatio * 100).toFixed(0)}% of days`
                    : "-"
                }
                hint="Share of historical days this item moves on this route"
              />
              <Stat
                label="Typical gap"
                value={avgGapDays != null ? `${avgGapDays.toFixed(0)} days` : "-"}
                hint="Average wait between purchases"
              />
            </div>
          </div>
        )}

        {/* Section 3: Load vs sold (only when actuals exist).
            NOT model accuracy -- compares the truck weight to invoiced
            units. Raw forecast accuracy is on the Pipeline page. */}
        {actual != null && (
          <div>
            <SectionTitle>How it performed</SectionTitle>
            <div className={GRID_3}>
              <Stat
                label="Actually sold"
                value={Math.round(actual).toLocaleString()}
                hint="Units invoiced on this date"
              />
              <Stat
                label="Load vs sold"
                value={
                  variancePct != null ? (
                    <Badge tone={accuracyTone(variancePct)}>
                      {variancePct > 0 ? "+" : ""}
                      {variancePct.toFixed(1)}%
                    </Badge>
                  ) : (
                    "-"
                  )
                }
                hint={
                  variance != null
                    ? `${variance > 0 ? "Sold" : "Loaded"} ${Math.abs(Math.round(variance)).toLocaleString()} ${variance > 0 ? "more than we recommended" : "more than sold"}`
                    : "Difference between truck load and invoiced units"
                }
              />
            </div>
          </div>
        )}

        {/* Section 3: Demand history (rolling windows) */}
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
