import Modal from "./Modal";
import Badge from "./Badge";
import ConfidenceBadge from "./ConfidenceBadge";
import {
  ExplainHeader,
  GRID_3,
  GRID_4,
  MODAL_BODY,
  SectionTitle,
  Stat,
  num,
  str,
} from "./explain/atoms";
import { pickDate } from "@/lib/format";
import { accuracyTone, varianceTone } from "@/lib/colorize";

import { useItemStats } from "@/hooks/useDataImport";
import type { ItemStatsWindow } from "@/types/data-import";
import type { Row } from "@/types/common";

interface Props {
  open: boolean;
  onClose: () => void;
  row: Row | null;
}

const TONE_TEXT: Record<"success" | "danger" | "neutral", string> = {
  success: "text-success-600",
  danger:  "text-danger-600",
  neutral: "",
};

function varianceClass(v: number): string {
  const tone = varianceTone(v);
  if (tone === "success" || tone === "danger") return TONE_TEXT[tone];
  return TONE_TEXT.neutral;
}

function classifyCV2(cv2: number | null): string {
  if (cv2 == null) return "unknown";
  if (cv2 < 0.5) return "low variability";
  if (cv2 < 1.0) return "moderate variability";
  return "high variability";
}

function classifyADI(adi: number | null): string {
  if (adi == null) return "";
  if (adi <= 1.2) return "frequent demand";
  if (adi <= 2.0) return "regular, occasional gaps";
  if (adi <= 4.0) return "intermittent demand";
  return "sparse demand";
}

function classDesc(cls: string): string {
  const c = cls.toLowerCase();
  if (c === "smooth") return "Stable demand, predictable.";
  if (c === "intermittent") return "Gaps between events; stable sizes.";
  if (c === "erratic") return "Frequent demand, variable quantities.";
  if (c === "lumpy") return "Hard to predict, high uncertainty.";
  return "";
}

function WindowStat({ label, w }: { label: string; w: ItemStatsWindow | null | undefined }) {
  if (!w || w.avg == null) {
    return <Stat label={label} value="-" hint="No demand in window" />;
  }
  return (
    <Stat
      label={label}
      value={
        <>
          {w.avg.toFixed(1)}
          <span className="text-xs font-normal text-text-tertiary"> /demand day</span>
        </>
      }
      hint={`${w.active_days} demand days / ${w.days}d - total ${w.total.toFixed(0)}`}
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
  const qtyIfDemand = num(row.qty_if_demand);
  const q10 = num(row.q_10 ?? row.lower_bound);
  const q90 = num(row.q_90 ?? row.upper_bound);
  const cls = str(row.class ?? row.demand_class);
  const adi = num(row.adi);
  const cv2 = num(row.cv2);
  const nonzeroRatio = num(row.nonzero_ratio);

  const stats = useItemStats(open && itemCode ? itemCode : undefined, routeCode || undefined);
  const windows = stats.data?.windows;

  const variance = predicted != null && actual != null ? actual - predicted : null;
  const variancePct =
    variance != null && predicted && predicted > 0 ? (variance / predicted) * 100 : null;

  const classVariant = (() => {
    const c = cls.toLowerCase();
    if (c === "smooth") return "info" as const;
    if (c === "intermittent") return "warning" as const;
    if (c === "erratic") return "danger" as const;
    return "neutral" as const;
  })();

  return (
    <Modal open={open} onClose={onClose} title="Why this forecast" size="xl">
      <div className={MODAL_BODY}>
        <ExplainHeader
          left={{ label: "Item", primary: itemCode, secondary: itemName }}
          right={{ label: "Route / Date", primary: routeCode, secondary: date }}
        />

        <div>
          <SectionTitle>Prediction</SectionTitle>
          <div className={GRID_4}>
            <Stat
              label="Predicted"
              value={predicted != null ? predicted.toFixed(1) : "-"}
              hint="Best single estimate"
            />
            <Stat
              label="Chance of selling"
              value={<ConfidenceBadge value={pDemand} />}
              hint="How likely the customer buys today"
            />
            <Stat
              label="Likely range"
              value={q10 != null && q90 != null ? `${q10.toFixed(1)} - ${q90.toFixed(1)}` : "-"}
              hint="Low-to-high estimate"
            />
            <Stat
              label="If they buy"
              value={qtyIfDemand != null ? qtyIfDemand.toFixed(1) : "-"}
              hint="Expected quantity when they do buy"
            />
          </div>
        </div>

        {actual != null && (
          <div>
            <SectionTitle>Actual performance</SectionTitle>
            <div className={GRID_3}>
              <Stat label="Actual" value={actual.toFixed(1)} hint="Sold" />
              <Stat
                label="Difference"
                value={
                  variance != null ? (
                    <span className={varianceClass(variance)}>
                      {variance > 0 ? "+" : ""}
                      {variance.toFixed(1)}
                    </span>
                  ) : (
                    "-"
                  )
                }
                hint="Actual minus recommended"
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
                hint="How far off in percent"
              />
            </div>
          </div>
        )}

        <div>
          <SectionTitle right={stats.loading ? "loading..." : undefined}>
            Rolling demand
          </SectionTitle>
          {stats.data?.available === false ? (
            <div className="text-sm text-text-tertiary bg-surface-sunken rounded-lg px-3 py-2 border border-subtle">
              {stats.data.message ?? "No historical sales"}
            </div>
          ) : (
            <div className={GRID_4}>
              <WindowStat label="Last week" w={windows?.last_week} />
              <WindowStat label="Last 4 weeks" w={windows?.last_4_weeks} />
              <WindowStat label="Last 3 months" w={windows?.last_3_months} />
              <WindowStat label="Last 6 months" w={windows?.last_6_months} />
            </div>
          )}
        </div>

        <div>
          <SectionTitle>Buying pattern</SectionTitle>
          <div className={GRID_4}>
            <Stat
              label="Pattern type"
              value={cls ? <Badge tone={classVariant}>{cls}</Badge> : "-"}
              hint={classDesc(cls)}
            />
            <Stat
              label="Days with sales"
              value={nonzeroRatio != null ? `${(nonzeroRatio * 100).toFixed(0)}%` : "-"}
              hint="Share of days the item sells"
            />
            <Stat
              label="Sale frequency"
              value={adi != null ? adi.toFixed(2) : "-"}
              hint={classifyADI(adi)}
            />
            <Stat
              label="Qty variability"
              value={cv2 != null ? cv2.toFixed(2) : "-"}
              hint={classifyCV2(cv2)}
            />
          </div>
        </div>
      </div>
    </Modal>
  );
}
