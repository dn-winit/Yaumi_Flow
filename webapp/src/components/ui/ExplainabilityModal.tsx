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
  const c = cls.toLowerCase();
  if (c === "smooth") return "Stable, predictable demand";
  if (c === "intermittent") return "Gaps between purchases, stable sizes";
  if (c === "erratic") return "Frequent but variable quantities";
  if (c === "lumpy") return "Infrequent and variable — harder to predict";
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
  return (
    <Stat
      label={label}
      value={
        <>
          {w.avg.toFixed(1)}
          <span className="text-caption font-normal text-text-tertiary"> /day</span>
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

  const predicted = num(row.prediction ?? row.predicted);
  const actual = num(row.actual_qty ?? row.TotalQuantity);
  const pDemand = num(row.p_demand);
  const q10 = num(row.q_10 ?? row.lower_bound);
  const q90 = num(row.q_90 ?? row.upper_bound);
  const cls = str(row.class ?? row.demand_class);
  const nonzeroRatio = num(row.nonzero_ratio);

  const stats = useItemStats(open && itemCode ? itemCode : undefined, routeCode || undefined);
  const windows = stats.data?.windows;

  const variance = predicted != null && actual != null ? actual - predicted : null;
  const variancePct =
    variance != null && predicted && predicted > 0 ? (variance / predicted) * 100 : null;

  return (
    <Modal open={open} onClose={onClose} title="Why this forecast" size="xl">
      <div className={MODAL_BODY}>
        <ExplainHeader
          left={{ label: "Item", primary: itemCode, secondary: itemName }}
          right={{ label: "Route / Date", primary: routeCode, secondary: date }}
        />

        {/* Section 1: The forecast itself */}
        <div>
          <SectionTitle>
            Forecast
            {cls && (
              <Badge tone={patternTone(cls)} className="ml-2 text-caption">
                {cls} — {classDesc(cls)}
              </Badge>
            )}
          </SectionTitle>
          <div className={GRID_3}>
            <Stat
              label="Predicted quantity"
              value={predicted != null ? predicted.toFixed(1) : "-"}
              hint="Best estimate for this item on this date"
            />
            <Stat
              label="Confidence"
              value={<ConfidenceBadge value={pDemand} />}
              hint="How likely this item sells today"
            />
            <Stat
              label="Likely range"
              value={q10 != null && q90 != null ? `${q10.toFixed(1)} – ${q90.toFixed(1)}` : "-"}
              hint="Low-to-high estimate covering most outcomes"
            />
          </div>
        </div>

        {/* Section 2: Actual vs forecast (only when actuals exist) */}
        {actual != null && (
          <div>
            <SectionTitle>How it performed</SectionTitle>
            <div className={GRID_3}>
              <Stat
                label="Actually sold"
                value={actual.toFixed(1)}
                hint="Units invoiced on this date"
              />
              <Stat
                label="Accuracy"
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
                    ? `${variance > 0 ? "Sold" : "Loaded"} ${Math.abs(variance).toFixed(1)} ${variance > 0 ? "more than predicted" : "more than sold"}`
                    : "Difference vs prediction"
                }
              />
              <Stat
                label="Days with sales"
                value={nonzeroRatio != null ? `${(nonzeroRatio * 100).toFixed(0)}%` : "-"}
                hint="Share of days this item sells on this route"
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
