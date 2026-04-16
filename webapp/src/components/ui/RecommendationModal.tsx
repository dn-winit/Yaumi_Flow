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
import { pickDate } from "@/lib/format";
import type { Row } from "@/types/common";

interface Props {
  open: boolean;
  onClose: () => void;
  row: Row | null;
}

interface SignalDict {
  kind: string;
  detail: string;
  weight: number;
  evidence?: Record<string, unknown>;
}

function cycleHint(cycle: number | null, daysSince: number | null): string {
  if (cycle == null || daysSince == null) return "";
  const overdue = daysSince - cycle;
  if (overdue > cycle * 0.25) return `Overdue by ${Math.round(overdue)}d`;
  if (overdue > 0) return `Due (past cycle by ${Math.round(overdue)}d)`;
  return `${Math.round(Math.abs(overdue))}d until next expected purchase`;
}

function parseSignals(raw: unknown): SignalDict[] {
  if (!raw) return [];
  if (Array.isArray(raw)) return raw as SignalDict[];
  if (typeof raw === "string") {
    try {
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? (parsed as SignalDict[]) : [];
    } catch {
      return [];
    }
  }
  return [];
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

  const signals = parseSignals((row as Record<string, unknown>).Signals);
  const whyItem = str((row as Record<string, unknown>).WhyItem);
  const whyQuantity = str((row as Record<string, unknown>).WhyQuantity);
  const confidence = num((row as Record<string, unknown>).Confidence);
  const source = str((row as Record<string, unknown>).Source);

  const hasExplain = signals.length > 0 || whyItem || whyQuantity;

  // Legacy fallback: reason fields from pre-Sprint-1 recs still in the DB
  const legacyReason = str((row as Record<string, unknown>).ReasonExplanation);
  const legacyConf = num((row as Record<string, unknown>).ReasonConfidence);

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
            secondary: date,
          }}
        />

        {/* Section 1: The recommendation itself */}
        <div>
          <SectionTitle>Recommendation</SectionTitle>
          <div className={GRID_3}>
            <Stat
              label="Recommended quantity"
              value={recommended != null ? recommended.toLocaleString() : "-"}
              hint="Units to load for this customer"
            />
            <Stat
              label="Band"
              value={tier ? <Badge tone={tierTone}>{tier.replace(/_/g, " ")}</Badge> : "-"}
              hint="Ranked priority across the route"
            />
            <Stat
              label="Van carrying"
              value={vanLoad != null ? vanLoad.toLocaleString() : "-"}
              hint="Total of this item on the van today"
            />
          </div>
        </div>

        {/* Section 2: AI explanation (Sprint-1+ recs) */}
        {hasExplain && (
          <div>
            <SectionTitle>Why this was recommended</SectionTitle>
            <div className="bg-brand-50 border border-brand-100 rounded-lg px-4 py-3 space-y-3">
              {whyItem && (
                <div>
                  <p className="text-body font-semibold text-brand-700">{whyItem}</p>
                  <p className="text-caption text-brand-600 mt-0.5">
                    {source && <span className="mr-2">Source: {source}</span>}
                    {confidence != null && confidence > 0 && (
                      <span>Confidence {(confidence * 100).toFixed(0)}%</span>
                    )}
                  </p>
                </div>
              )}

              {signals.length > 0 && (
                <div className="space-y-2">
                  {signals.map((s, i) => (
                    <div key={`${s.kind}-${i}`}>
                      <div className="flex items-center justify-between text-caption mb-0.5">
                        <span className="text-brand-700 leading-snug">{s.detail}</span>
                        <span className="text-brand-600 tabular-nums shrink-0 ml-2">
                          {Math.round((s.weight || 0) * 100)}%
                        </span>
                      </div>
                      <div className="h-1.5 rounded-full bg-brand-100 overflow-hidden">
                        <div
                          className="h-full rounded-full bg-brand-500"
                          style={{ width: `${Math.round((s.weight || 0) * 100)}%` }}
                        />
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {whyQuantity && (
                <div className="pt-1 border-t border-brand-100">
                  <p className="text-caption uppercase tracking-wide text-brand-700 font-semibold">
                    How we sized this
                  </p>
                  <p className="text-caption text-brand-700 mt-0.5 leading-relaxed">
                    {whyQuantity}
                  </p>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Legacy fallback: pre-Sprint-1 recs only have ReasonExplanation */}
        {!hasExplain && legacyReason && (
          <div>
            <SectionTitle>Reason</SectionTitle>
            <div className="bg-brand-50 border border-brand-100 rounded-lg px-4 py-3">
              <p className="text-body text-brand-700">{legacyReason}</p>
              {legacyConf != null && (
                <p className="text-caption text-brand-600 mt-1">
                  Confidence {legacyConf}%
                </p>
              )}
            </div>
          </div>
        )}

        {/* Section 3: Customer context (only the 3 actionable stats) */}
        <div>
          <SectionTitle>Customer context</SectionTitle>
          <div className={GRID_3}>
            <Stat
              label="Avg qty per visit"
              value={avgQty != null ? avgQty.toLocaleString() : "-"}
              hint="Historical average when this customer buys this item"
            />
            <Stat
              label="Buying cycle"
              value={cycleDays != null ? `Every ${cycleDays.toFixed(0)} days` : "-"}
              hint="Typical gap between purchases of this item"
            />
            <Stat
              label="Last purchased"
              value={daysSince != null ? `${daysSince} days ago` : "-"}
              hint={cycleHint(cycleDays, daysSince)}
            />
          </div>
        </div>
      </div>
    </Modal>
  );
}
