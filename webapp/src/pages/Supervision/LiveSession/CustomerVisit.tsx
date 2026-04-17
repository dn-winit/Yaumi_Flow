import { useState } from "react";
import { supervisionApi } from "@/api/supervision";
import { analyticsApi } from "@/api/analytics";
import Badge from "@/components/ui/Badge";
import Button from "@/components/ui/Button";
import Loading from "@/components/ui/Loading";
import Modal from "@/components/ui/Modal";
import AnalysisList from "./AnalysisList";

interface CustomerItem {
  itemCode: string;
  itemName?: string;
  recommendedQty: number;
  tier?: string;
  source?: string;
  whyItem?: string;
  whyQuantity?: string;
  purchaseCycleDays?: number;
  daysSinceLastPurchase?: number;
  frequencyPercent?: number;
  trendFactor?: number;
}

interface VisitScore {
  score: number;
  coverage: number;
  accuracy: number;
}

interface Props {
  sessionId: string;
  routeCode: string;
  date: string;
  customerCode: string;
  customerName: string;
  items: CustomerItem[];
  /** True when YaumiLive shows an invoice today, even if the supervisor hasn't
   *  clicked "Visit" in this session yet. Drives the small green dot on the row. */
  liveVisited?: boolean;
  onVisitComplete: (result: Record<string, unknown>) => void;
  onRequestAnalysis: (payload: {
    customerCode: string;
    customerName: string;
    items: { itemCode: string; itemName?: string; recommendedQuantity: number; actualQuantity: number }[];
    score: VisitScore;
  }) => void;
}

function scoreBadgeVariant(score: number): "success" | "warning" | "danger" | "neutral" {
  if (score >= 85) return "success";
  if (score >= 65) return "warning";
  if (score > 0) return "danger";
  return "neutral";
}

export default function CustomerVisit({
  sessionId,
  routeCode,
  date,
  customerCode,
  customerName,
  items,
  liveVisited = false,
  onVisitComplete,
  onRequestAnalysis,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [visitResult, setVisitResult] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [briefing, setBriefing] = useState<Record<string, unknown> | null>(null);
  const [briefingOpen, setBriefingOpen] = useState(false);
  const [briefingLoading, setBriefingLoading] = useState(false);

  const score = visitResult?.score as VisitScore | undefined;
  const visited = !!score;

  // Actuals are authored by the backend (pulled live from YaumiLive) and
  // returned in the visit payload. Missing keys mean "zero sold for that item".
  const actuals = (visitResult?.actualSales ?? {}) as Record<string, number>;

  const redistributions = (visitResult?.redistributions ?? []) as {
    from: string; to: string; itemCode: string; quantity: number;
  }[];

  // Items the customer bought today that were NOT on their plan --
  // awareness-only, doesn't affect score.
  const alsoBought = (visitResult?.alsoBought ?? []) as { item_code: string; qty: number }[];

  const handleVisit = async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await supervisionApi.processVisit(sessionId, customerCode);
      const v = res.visit as unknown as Record<string, unknown>;
      if (v.error) {
        setError(String(v.error));
        return;
      }
      setVisitResult(v);
      setExpanded(true); // auto-reveal the filled-in actuals
      onVisitComplete(v);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to process visit");
    } finally {
      setLoading(false);
    }
  };

  const handleBriefing = async () => {
    setBriefingLoading(true);
    setBriefingOpen(true);
    try {
      const res = await analyticsApi.preVisitBriefing({
        customer_code: customerCode,
        customer_name: customerName,
        route_code: routeCode,
        date,
        items: items.map((i) => ({
          itemCode: i.itemCode,
          itemName: i.itemName,
          recommendedQty: i.recommendedQty,
          tier: i.tier,
          source: i.source,
          whyItem: i.whyItem,
          whyQuantity: i.whyQuantity,
          purchaseCycleDays: i.purchaseCycleDays,
          daysSinceLastPurchase: i.daysSinceLastPurchase,
          frequencyPercent: i.frequencyPercent,
          trendFactor: i.trendFactor,
        })),
      });
      setBriefing(res.data);
    } catch {
      setBriefing({ briefing: "Briefing unavailable. Please try again.", key_items: [], heads_up: "" });
    } finally {
      setBriefingLoading(false);
    }
  };

  const handleAiClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    onRequestAnalysis({
      customerCode,
      customerName,
      items: items.map((i) => ({
        itemCode: i.itemCode,
        itemName: i.itemName,
        recommendedQuantity: i.recommendedQty,
        actualQuantity: actuals[i.itemCode] ?? 0,
      })),
      score: score ?? { score: 0, coverage: 0, accuracy: 0 },
    });
  };

  return (
    <div className="border border-default rounded-xl bg-surface-raised overflow-hidden">
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center justify-between gap-3 px-4 py-3 hover:bg-surface-sunken transition-colors text-left"
      >
        <div className="flex-1 min-w-0 flex items-center gap-2">
          {(visited || liveVisited) && (
            <span
              className="w-2 h-2 rounded-full bg-success-500 shrink-0"
              title={visited ? "Visit processed in this session" : "Customer invoiced today (live from YaumiLive)"}
              aria-label="visited"
            />
          )}
          <div className="min-w-0">
            <p className="text-body font-semibold text-text-primary truncate">{customerName || customerCode}</p>
            <p className="text-caption text-text-tertiary">{customerCode}</p>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <Badge variant="neutral">{items.length} items</Badge>
          {visited ? (
            <Badge variant={scoreBadgeVariant(score!.score)}>Visited - {score!.score.toFixed(0)}%</Badge>
          ) : liveVisited ? (
            <Badge variant="success">Visited live</Badge>
          ) : (
            <Badge variant="info">Pending</Badge>
          )}
          {visited && (
            <button
              onClick={handleAiClick}
              title="AI review"
              className="p-1.5 rounded-lg hover:bg-brand-50 text-brand-600 transition-colors"
              aria-label="AI review"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z" />
              </svg>
            </button>
          )}
          <svg
            className={`w-4 h-4 text-text-tertiary transition-transform ${expanded ? "rotate-180" : ""}`}
            fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
          </svg>
        </div>
      </button>

      {expanded && (
        <div className="border-t border-default bg-surface-sunken/40 px-4 py-3 space-y-3">
          <table className="w-full text-body">
            <thead>
              <tr className="text-left text-caption font-medium text-text-tertiary uppercase tracking-wide">
                <th className="px-2 py-2 w-32">Item code</th>
                <th className="px-2 py-2">Name</th>
                <th className="px-2 py-2 w-28 text-right">Recommended</th>
                <th className="px-2 py-2 w-28 text-right">Actual (live)</th>
                <th className="px-2 py-2 w-24 text-right">Delta</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-subtle">
              {items.map((it) => {
                const actual = visited ? (actuals[it.itemCode] ?? 0) : null;
                const delta = actual != null ? actual - it.recommendedQty : null;
                return (
                  <tr key={it.itemCode}>
                    <td className="px-2 py-2 font-medium text-text-primary">{it.itemCode}</td>
                    <td className="px-2 py-2 text-text-secondary">{it.itemName ?? "-"}</td>
                    <td className="px-2 py-2 text-right text-text-secondary">{it.recommendedQty}</td>
                    <td className="px-2 py-2 text-right">
                      {actual == null ? (
                        <span className="text-text-tertiary">--</span>
                      ) : (
                        <span className="font-medium text-text-primary">{actual}</span>
                      )}
                    </td>
                    <td className="px-2 py-2 text-right">
                      {delta == null ? (
                        <span className="text-text-tertiary">--</span>
                      ) : delta === 0 ? (
                        <span className="text-text-tertiary">0</span>
                      ) : delta > 0 ? (
                        <span className="text-success-600">+{delta}</span>
                      ) : (
                        <span className="text-danger-600">{delta}</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          {!visited && (
            <div className="flex items-center gap-3">
              <Button variant="secondary" size="sm" loading={briefingLoading} onClick={handleBriefing}>
                Briefing
              </Button>
              <Button
                variant="primary"
                size="sm"
                loading={loading}
                disabled={!briefing}
                className={briefing ? "" : "opacity-40"}
                onClick={handleVisit}
              >
                Mark visited
              </Button>
              {error && <span className="text-caption text-danger-600">{error}</span>}
            </div>
          )}

          {visited && score && (
            <div className="flex items-center gap-5 text-body bg-brand-50 border border-brand-100 rounded-lg px-3 py-2">
              <span title="Weighted visit score combining coverage and quantity accuracy">
                <span className="text-brand-700 font-semibold">{score.score.toFixed(1)}%</span>
                <span className="text-brand-600 ml-1 text-caption">overall</span>
              </span>
              <span title="Share of recommended items the customer actually bought">
                <span className="text-brand-700 font-semibold">{score.coverage.toFixed(1)}%</span>
                <span className="text-brand-600 ml-1 text-caption">items matched</span>
              </span>
              <span title="How close the actual quantities were to what we recommended">
                <span className="text-brand-700 font-semibold">{score.accuracy.toFixed(1)}%</span>
                <span className="text-brand-600 ml-1 text-caption">qty accuracy</span>
              </span>
            </div>
          )}

          {alsoBought.length > 0 && (
            <div>
              <p className="text-caption font-medium text-text-tertiary uppercase tracking-wider mb-1">
                Also bought (not on plan)
              </p>
              <div className="flex flex-wrap gap-2">
                {alsoBought.map((r) => (
                  <div
                    key={r.item_code}
                    className="inline-flex items-center gap-2 text-caption bg-warning-50 border border-warning-100 text-warning-700 rounded-full px-2 py-1"
                  >
                    <span className="font-medium">{r.item_code}</span>
                    <span className="text-warning-700">x {r.qty}</span>
                  </div>
                ))}
              </div>
              <p className="mt-1 text-caption text-text-tertiary">
                Off-plan purchases don't affect the score -- shown for visibility only.
              </p>
            </div>
          )}

          {redistributions.length > 0 && (
            <div>
              <p className="text-caption font-medium text-text-tertiary uppercase tracking-wider mb-1">Moved to other customers</p>
              <div className="space-y-1">
                {redistributions.map((r, idx) => (
                  <div key={idx} className="flex items-center gap-2 text-caption text-text-secondary">
                    <Badge variant="info">{r.itemCode}</Badge>
                    <span>{r.quantity} units: {r.from} &rarr; {r.to}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      <Modal
        open={briefingOpen}
        onClose={() => setBriefingOpen(false)}
        title={`Pre-visit briefing — ${customerName || customerCode}`}
        size="xl"
      >
        {briefingLoading ? (
          <Loading message="Preparing briefing..." />
        ) : briefing ? (
          <div className="space-y-5">
            <div className="flex flex-wrap items-center gap-3 pb-3 border-b border-subtle">
              <span className="text-body text-text-tertiary">Route</span>
              <Badge variant="info">{routeCode}</Badge>
              <span className="text-body text-text-tertiary">Date</span>
              <Badge variant="neutral">{date}</Badge>
              <span className="ml-auto text-body text-text-tertiary">
                {items.length} items · {items.reduce((n, i) => n + i.recommendedQty, 0)} units
              </span>
            </div>

            {typeof briefing.briefing === "string" && briefing.briefing && (
              <div className="bg-surface-sunken rounded-lg border border-subtle px-4 py-3 text-body text-text-secondary leading-relaxed">
                {briefing.briefing}
              </div>
            )}

            <AnalysisList
              title="Key items to push"
              tone="success"
              items={Array.isArray(briefing.key_items) ? briefing.key_items.map(String) : []}
            />
            <AnalysisList
              title="Heads up"
              tone="warning"
              items={typeof briefing.heads_up === "string" && briefing.heads_up ? [briefing.heads_up] : []}
            />
          </div>
        ) : null}
      </Modal>
    </div>
  );
}
