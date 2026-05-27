import Modal from "./Modal";
import Badge from "./Badge";
import {
  ExplainHeader,
  GRID_2,
  GRID_3,
  MODAL_BODY,
  SectionTitle,
  Stat,
  num,
  str,
} from "./explain/atoms";
import { pickDate, fmtNum } from "@/lib/format";
import { fmtDate } from "@/lib/date";
import type { Row } from "@/types/common";

interface Props {
  open: boolean;
  onClose: () => void;
  row: Row | null;
}

function cycleHint(cycle: number | null, daysSince: number | null): string {
  // Plain English (no "past cycle" / "d" abbreviations).
  if (cycle == null || cycle <= 0 || daysSince == null) return "";
  const overdue = daysSince - cycle;
  if (overdue > cycle * 0.25) {
    return `Overdue by ${Math.round(overdue)} days`;
  }
  if (overdue > 0) {
    return `Slightly overdue — ${Math.round(overdue)} days past the usual gap`;
  }
  return `Next purchase expected in ${Math.round(Math.abs(overdue))} days`;
}

function sourceLabel(source: string): { label: string; tone: "info" | "success" | "warning" | "neutral" } {
  // Maps the canonical strings from core/explain.py to plain English.
  const s = source.toLowerCase();
  if (s === "history")      return { label: "From this customer's history",          tone: "success" };
  if (s === "peer")         return { label: "From similar customers' purchases",     tone: "info" };
  if (s === "basket")       return { label: "Often bought with another item ordered", tone: "info" };
  if (s === "reactivation") return { label: "Customer hasn't ordered recently",       tone: "warning" };
  if (s === "seed")         return { label: "First-visit suggestion — popular item",  tone: "neutral" };
  return { label: source, tone: "neutral" };
}

export default function RecommendationModal({ open, onClose, row }: Props) {
  if (!row) return null;

  const itemCode = str(row.ItemCode);
  const itemName = str(row.ItemName);
  const customerCode = str(row.CustomerCode);
  const customerName = str(row.CustomerName);
  const date = pickDate(row);

  const recommended = num(row.RecommendedQuantity);
  const vanLoad = num(row.VanLoad);

  const avgQty = num(row.AvgQuantityPerVisit);
  const cycleDays = num(row.PurchaseCycleDays);
  const daysSince = num(row.DaysSinceLastPurchase);

  const whyItem = str(row.WhyItem);
  const whyQuantity = str(row.WhyQuantity);
  const source = str(row.Source);

  // PurchaseCount==0 -> never bought; render em-dash instead of "0 days ago" / "Every 0 days".
  const purchaseCount = num(row.PurchaseCount);
  const isFirstTime = purchaseCount == null || purchaseCount === 0;

  const reasonText = whyItem;
  const sizingText = whyQuantity;
  const hasNarrative = !!(reasonText || sizingText);

  return (
    <Modal open={open} onClose={onClose} title="Why this recommendation" size="xl">
      <div className={MODAL_BODY}>
        <ExplainHeader
          left={{ label: "Item", primary: itemCode, secondary: itemName }}
          right={{
            label: "Customer / Date",
            primary: customerCode + (customerName ? ` — ${customerName}` : ""),
            secondary: fmtDate(date),
          }}
        />

        {/* Section 1: headline numbers; source chip = the generator lane in plain English. */}
        <div>
          <SectionTitle
            right={
              source ? (
                <Badge tone={sourceLabel(source).tone} className="text-caption">
                  {sourceLabel(source).label}
                </Badge>
              ) : undefined
            }
          >
            Recommendation
          </SectionTitle>
          <div className={GRID_2}>
            <Stat
              label="Recommended quantity"
              value={recommended != null ? fmtNum(recommended) : "-"}
              hint="Units to load for this customer"
              highlight
            />
            <Stat
              label="Total on van today"
              value={vanLoad != null ? fmtNum(vanLoad) : "-"}
              hint="All units of this item on the route's truck today"
            />
          </div>
        </div>

        {/* Section 2: customer pattern; first-time recs swap in placeholder copy. */}
        <div>
          <SectionTitle>Customer pattern</SectionTitle>
          {isFirstTime ? (
            <div className={GRID_3}>
              <Stat
                label="Avg quantity per visit"
                value="—"
                hint="First-time suggestion (no past purchase)"
              />
              <Stat
                label="Buying cycle"
                value="—"
                hint="No personal cycle established yet"
              />
              <Stat
                label="Last purchased"
                value="Never"
                hint="See ‘Why we suggested it’ below"
              />
            </div>
          ) : (
            <div className={GRID_3}>
              <Stat
                label="Avg quantity per visit"
                value={avgQty != null ? fmtNum(avgQty) : "-"}
                hint="Historical average when this customer buys this item"
              />
              <Stat
                label="Buying cycle"
                value={cycleDays != null && cycleDays > 0 ? `Every ${cycleDays.toFixed(0)} days` : "-"}
                hint="Typical gap between purchases of this item"
              />
              <Stat
                label="Last purchased"
                value={daysSince != null ? `${daysSince} days ago` : "-"}
                hint={cycleHint(cycleDays, daysSince)}
              />
            </div>
          )}
        </div>

        {/* Section 3: prose-only "why" -- two engine-written sentences (item + quantity). */}
        {hasNarrative && (
          <div>
            <SectionTitle>Why we suggested it</SectionTitle>
            <div className={GRID_3}>
              <Stat label="Why this item" value={reasonText || "—"} prose />
              {sizingText && (
                <Stat label="How we sized it" value={sizingText} prose />
              )}
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
