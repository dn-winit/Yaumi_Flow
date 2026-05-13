import Modal from "./Modal";
import Badge from "./Badge";
import {
  ExplainHeader,
  GRID_3,
  MODAL_BODY,
  SectionTitle,
  Stat,
  num,
  str,
} from "./explain/atoms";
import { pickDate, fmtNum, fmtPct } from "@/lib/format";
import { fmtDate } from "@/lib/date";
import type { Row } from "@/types/common";

interface Props {
  open: boolean;
  onClose: () => void;
  row: Row | null;
}

function cycleHint(cycle: number | null, daysSince: number | null): string {
  // Skip when there's no real cycle to compare against -- otherwise an
  // unknown cycle (0) compared with any days_since trips the "Due (past
  // cycle by Nd)" branch and prints nonsense.
  if (cycle == null || cycle <= 0 || daysSince == null) return "";
  const overdue = daysSince - cycle;
  if (overdue > cycle * 0.25) return `Overdue by ${Math.round(overdue)}d`;
  if (overdue > 0) return `Due (past cycle by ${Math.round(overdue)}d)`;
  return `${Math.round(Math.abs(overdue))}d until next expected purchase`;
}

function tierDesc(tier: string): string {
  const t = tier.toUpperCase();
  if (t === "MUST_STOCK") return "Always carry — high-conviction recommendation";
  if (t === "SHOULD_STOCK") return "Carry when there's room — solid signal";
  if (t === "CONSIDER") return "Optional — weaker signal, judgment call";
  return "";
}

function sourceLabel(source: string): { label: string; tone: "info" | "success" | "warning" | "neutral" } {
  // Plain-language label for the engine's generator lane, matched to
  // the canonical strings emitted by core/explain.py.
  const s = source.toLowerCase();
  if (s === "history")      return { label: "From this customer's history",     tone: "success" };
  if (s === "peer")         return { label: "From similar customers' baskets",  tone: "info" };
  if (s === "basket")       return { label: "Pairs with another item ordered",  tone: "info" };
  if (s === "reactivation") return { label: "Reactivation — customer went quiet", tone: "warning" };
  if (s === "seed")         return { label: "First-visit seed — top route item", tone: "neutral" };
  return { label: source, tone: "neutral" };
}

interface SignalEntry {
  detail?: string;
  weight?: number;
}

function topSignals(signals: unknown): SignalEntry[] {
  // Engine ships an array of {kind, detail, weight, evidence}. We
  // surface up to 4 of the highest-weighted, dropping anything missing
  // a detail string so the list never renders empty rows.
  if (!Array.isArray(signals)) return [];
  const cleaned: SignalEntry[] = signals
    .filter((s): s is Record<string, unknown> => !!s && typeof s === "object")
    .map((s) => ({
      detail: typeof s.detail === "string" ? s.detail : undefined,
      weight: typeof s.weight === "number" ? s.weight : undefined,
    }))
    .filter((s) => s.detail);
  cleaned.sort((a, b) => (b.weight ?? 0) - (a.weight ?? 0));
  return cleaned.slice(0, 4);
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
  const tier = str(row.Tier);

  const avgQty = num(row.AvgQuantityPerVisit);
  const cycleDays = num(row.PurchaseCycleDays);
  const daysSince = num(row.DaysSinceLastPurchase);

  const whyItem = str((row as Record<string, unknown>).WhyItem);
  const whyQuantity = str((row as Record<string, unknown>).WhyQuantity);
  const confidence = num((row as Record<string, unknown>).Confidence);
  const source = str((row as Record<string, unknown>).Source);
  const signals = topSignals((row as Record<string, unknown>).Signals);

  // ``PurchaseCount == 0`` is the load-bearing contract from the engine:
  // the customer has never bought this item before. The customer-context
  // section's avg-qty / cycle / days-since fields are then placeholder
  // zeros (peer / basket / seed candidates fill them only when the
  // customer DOES have history). Surface that fact instead of rendering
  // misleading "0 days ago" / "Every 0 days".
  const purchaseCount = num((row as Record<string, unknown>).PurchaseCount);
  const isFirstTime = purchaseCount == null || purchaseCount === 0;

  const hasNarrative = !!(whyItem || whyQuantity);

  // Legacy fallback: reason fields from pre-explainability recs still in
  // the DB. The new pipeline always emits whyItem + whyQuantity, but old
  // rows might only carry ReasonExplanation. Render the same way.
  const legacyReason = str((row as Record<string, unknown>).ReasonExplanation);
  const reasonText = whyItem || legacyReason;
  const sizingText = whyQuantity;

  const tierTone = (() => {
    const t = tier.toUpperCase();
    if (t === "MUST_STOCK") return "success" as const;
    if (t === "SHOULD_STOCK") return "info" as const;
    if (t === "CONSIDER") return "warning" as const;
    return "neutral" as const;
  })();

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

        {/* Section 1: Recommendation -- mirrors the forecast modal's
            structure. Tier badge inline; source chip on the right tells
            the supervisor *which generator* picked this item, the
            single most useful piece of provenance. */}
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
            {tier && (
              <Badge tone={tierTone} className="ml-2 text-caption">
                {tier.replace(/_/g, " ")}
              </Badge>
            )}
          </SectionTitle>
          {tier && tierDesc(tier) && (
            <p className="text-caption text-text-tertiary -mt-2 mb-2">{tierDesc(tier)}</p>
          )}
          <div className={GRID_3}>
            <Stat
              label="Recommended quantity"
              value={recommended != null ? fmtNum(recommended) : "-"}
              hint="Units to load for this customer"
              highlight
            />
            <Stat
              label="Confidence"
              value={
                confidence != null && confidence > 0
                  ? fmtPct(confidence * 100, 0)
                  : "-"
              }
              hint="Strength of the signals behind this suggestion"
            />
            <Stat
              label="Total on van today"
              value={vanLoad != null ? fmtNum(vanLoad) : "-"}
              hint="All units of this item loaded on the route's truck today"
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

        {/* Section 3: Reason narrative -- mirrors the forecast modal's
            "How we got there" section structurally (3-col Stat grid).
            Uses the prose variant of Stat so WhyItem / WhyQuantity wrap
            inside the same outer card the numeric stats use, keeping the
            visual rhythm identical and avoiding any scrollable prose
            block. */}
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

        {/* Section 4: Structured signal breakdown. Backend ships an
            ordered array of {detail, weight}; we surface the top 4 with
            a thin weight bar so a supervisor can see which corroborating
            signals the engine stacked behind the headline reason. */}
        {signals.length > 0 && (
          <div>
            <SectionTitle>Supporting signals</SectionTitle>
            <ul className="space-y-2">
              {signals.map((s, i) => (
                <li
                  key={i}
                  className="flex items-center gap-3 text-body text-text-secondary"
                >
                  <div className="flex-1 truncate">{s.detail}</div>
                  {s.weight != null && (
                    <>
                      <div className="w-20 h-1.5 bg-surface-sunken rounded">
                        <div
                          className="h-full bg-brand-500 rounded"
                          style={{ width: `${Math.max(4, Math.min(100, s.weight * 100))}%` }}
                        />
                      </div>
                      <div className="w-10 text-right text-caption text-text-tertiary tabular-nums">
                        {(s.weight * 100).toFixed(0)}%
                      </div>
                    </>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </Modal>
  );
}
