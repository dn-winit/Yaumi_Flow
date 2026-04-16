import Modal from "./Modal";
import Badge from "./Badge";

import {
  ExplainHeader,
  GRID_2,
  GRID_3,
  GRID_4,
  MODAL_BODY,
  SectionTitle,
  Stat,
  num,
  str,
} from "./explain/atoms";
import { pickDate } from "@/lib/format";
import { churnTone, patternQualityTone, trendTone } from "@/lib/colorize";
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

function trendClass(trend: number): string {
  const tone = trendTone(trend);
  if (tone === "success") return "text-success-600";
  if (tone === "danger") return "text-danger-600";
  return "";
}

function cycleHint(cycle: number | null, daysSince: number | null): string {
  if (cycle == null || daysSince == null) return "";
  const overdue = daysSince - cycle;
  if (overdue > cycle * 0.25) return `Overdue by ${Math.round(overdue)}d`;
  if (overdue > 0) return `Due (past cycle by ${Math.round(overdue)}d)`;
  return `Early by ${Math.round(Math.abs(overdue))}d`;
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
  const priority = num(row.PriorityScore);
  const vanLoad = num(row.VanLoad);

  const avgQty = num(row.AvgQuantityPerVisit);
  const cycleDays = num(row.PurchaseCycleDays);
  const daysSince = num(row.DaysSinceLastPurchase);
  const freqPct = num(row.FrequencyPercent);
  const purchaseCount = num(row.PurchaseCount);
  const patternQuality = num(row.PatternQuality);

  const churn = num(row.ChurnProbability);
  const trend = num(row.TrendFactor);

  // Sprint-1 explainability fields (replace legacy ReasonExplanation)
  const signals = parseSignals((row as Record<string, unknown>).Signals);
  const whyItem = str((row as Record<string, unknown>).WhyItem);
  const whyQuantity = str((row as Record<string, unknown>).WhyQuantity);
  const confidence = num((row as Record<string, unknown>).Confidence);
  const source = str((row as Record<string, unknown>).Source);

  const churnToneValue = churnTone(churn);
  const patternToneValue = patternQualityTone(patternQuality);

  const hasNewExplain = signals.length > 0 || whyItem || whyQuantity;

  return (
    <Modal open={open} onClose={onClose} title="Recommendation Explainability" size="xl">
      <div className={MODAL_BODY}>
        <ExplainHeader
          left={{ label: "Item", primary: itemCode, secondary: itemName }}
          right={{
            label: "Customer / Date",
            primary: customerCode + (customerName ? ` - ${customerName}` : ""),
            secondary: date,
          }}
        />

        <div>
          <SectionTitle>Recommendation</SectionTitle>
          <div className={GRID_3}>
            <Stat
              label="Recommended"
              value={recommended != null ? recommended.toLocaleString() : "-"}
              hint="Units to load"
            />
            <Stat
              label="Priority"
              value={priority != null ? priority.toFixed(1) : "-"}
              hint="Ranking score"
            />
            <Stat
              label="Van Load"
              value={vanLoad != null ? vanLoad.toLocaleString() : "-"}
              hint="Forecasted total for route"
            />
          </div>
        </div>

        {hasNewExplain && (
          <div>
            <SectionTitle>Why this recommendation</SectionTitle>
            <div className="bg-brand-50 border border-brand-100 rounded-lg px-3 py-3 space-y-3">
              {whyItem && (
                <div>
                  <div className="text-sm font-semibold text-brand-700">{whyItem}</div>
                  <div className="text-[11px] text-brand-600 mt-0.5">
                    {source && <span className="mr-2">Source: {source}</span>}
                    {confidence != null && confidence > 0 && (
                      <span>Confidence {(confidence * 100).toFixed(0)}%</span>
                    )}
                  </div>
                </div>
              )}

              {signals.length > 0 && (
                <div className="space-y-1.5">
                  {signals.map((s, i) => (
                    <div key={`${s.kind}-${i}`}>
                      <div className="flex items-center justify-between text-[12px] mb-0.5">
                        <span className="font-medium text-brand-700">{s.kind}</span>
                        <span className="text-brand-600 tabular-nums">{Math.round((s.weight || 0) * 100)}%</span>
                      </div>
                      <div className="h-1.5 rounded-full bg-brand-100 overflow-hidden">
                        <div
                          className="h-full rounded-full bg-brand-500"
                          style={{ width: `${Math.round((s.weight || 0) * 100)}%` }}
                        />
                      </div>
                      <div className="text-[11px] text-brand-600 mt-0.5">{s.detail}</div>
                    </div>
                  ))}
                </div>
              )}

              {whyQuantity && (
                <div>
                  <div className="text-[11px] uppercase tracking-wide text-brand-700 font-semibold">
                    How we sized this
                  </div>
                  <div className="text-[12px] text-brand-700 mt-0.5 leading-snug">
                    {whyQuantity}
                  </div>
                </div>
              )}
            </div>
          </div>
        )}

        <div>
          <SectionTitle>Customer buying pattern</SectionTitle>
          <div className={GRID_4}>
            <Stat
              label="Avg qty / visit"
              value={avgQty != null ? avgQty.toLocaleString() : "-"}
              hint="Historical average when bought"
            />
            <Stat
              label="Days between buys"
              value={cycleDays != null ? `${cycleDays.toFixed(1)}d` : "-"}
              hint="Typical gap between purchases"
            />
            <Stat
              label="Days since last"
              value={daysSince != null ? `${daysSince}d` : "-"}
              hint={cycleHint(cycleDays, daysSince)}
            />
            <Stat
              label="Buy rate"
              value={freqPct != null ? `${freqPct.toFixed(1)}%` : "-"}
              hint="Share of visits where the item was bought"
            />
            <Stat
              label="Past purchases"
              value={purchaseCount != null ? purchaseCount.toLocaleString() : "-"}
              hint="Total times bought in history"
            />
            <Stat
              label="Buying consistency"
              value={
                patternQuality != null ? (
                  <Badge tone={patternToneValue}>{(patternQuality * 100).toFixed(0)}%</Badge>
                ) : (
                  "-"
                )
              }
              hint="How regular the buying pattern is"
            />
          </div>
        </div>

        {(churn != null || trend != null) && (
          <div>
            <SectionTitle>Trend &amp; risk</SectionTitle>
            <div className={GRID_2}>
              <Stat
                label="Drop-off risk"
                value={
                  churn != null ? (
                    <Badge tone={churnToneValue}>{(churn * 100).toFixed(0)}%</Badge>
                  ) : (
                    "-"
                  )
                }
                hint="Chance this customer stops buying the item"
              />
              <Stat
                label="Recent trend"
                value={
                  trend != null ? (
                    <span className={trendClass(trend)}>
                      {trend > 1 ? "+" : ""}
                      {((trend - 1) * 100).toFixed(0)}%
                    </span>
                  ) : (
                    "-"
                  )
                }
                hint="Recent sales vs long-run average"
              />
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
