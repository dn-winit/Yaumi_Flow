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
  // Plain English -- avoid jargon like "past cycle", short forms like
  // "d", and any other phrasing a route supervisor would have to decode.
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
  // Plain-language label for the engine's generator lane, matched to
  // the canonical strings emitted by core/explain.py.
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

  // PurchaseCount == 0 means the customer never bought this item. The
  // customer-context fields are placeholder zeros in that case (peer /
  // basket / seed candidates fill them only when history exists), so we
  // render an em-dash instead of "0 days ago" / "Every 0 days".
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

        {/* Section 1: the headline numbers. Source chip on the right is
            the one piece of provenance kept on this section -- it tells
            the supervisor which signal lane picked this customer in
            plain English. No tier badge: "MUST_STOCK" etc are internal
            labels that confuse rather than help. */}
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

        {/* Section 2: Customer pattern -- mirrors the forecast modal's
            "How we got there" section. 3 anchoring stats from the
            customer's own history. First-time recommendations replace the
            grid with a single line so we never render "0 days ago" /
            "Every 0 days" as if they were facts. */}
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

        {/* Section 3: the only "why" section -- two prose sentences the
            engine writes per row: one explains the item choice, the other
            explains the quantity. Numbered the Section 4 bullet list used
            to render is gone -- it pulled from the same Signals array
            that already feeds these two sentences, so it duplicated the
            story without adding new information. */}
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
