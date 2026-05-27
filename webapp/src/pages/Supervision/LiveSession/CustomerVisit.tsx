import { useEffect, useMemo, useState } from "react";
import { supervisionApi } from "@/api/supervision";
import { analyticsApi } from "@/api/analytics";
import Badge from "@/components/ui/Badge";
import Button from "@/components/ui/Button";
import Loading from "@/components/ui/Loading";
import Modal from "@/components/ui/Modal";
import RecommendedValue from "@/components/ui/RecommendedValue";
import { TABLE_SCROLL_CLASS } from "@/components/ui/Table";
import { fmtDate } from "@/lib/date";
import { fmtNum } from "@/lib/format";
import type { Row } from "@/types/common";
import type { RedistributionView } from "@/types/supervision";
import AnalysisList from "./AnalysisList";
import RedistributionSection from "./RedistributionSection";

interface CustomerItem {
  itemCode: string;
  itemName?: string;
  recommendedQty: number;
  // PascalCase rec from recommended_order; single source for the click-to-explain
  // popup AND the analytics payloads (no camelCase shadow).
  rec: Record<string, unknown>;
}

/**
 * Canonical builder for LLM-bound per-item payloads.
 *
 * Single source of truth used by BOTH the pre-visit briefing path AND the
 * post-visit customer analysis path. Spreads every field from the rec-engine
 * record (PascalCase) and overlays the runtime fields (itemCode/itemName/
 * recommendedQty/actualQty). The llm_analytics analyzer's ``_pick`` helper
 * accepts PascalCase, camelCase and snake_case keys interchangeably, so the
 * spread carries trend_factor / why_item / why_quantity / source / tier /
 * priority_score / purchase_cycle_days / days_since_last_purchase /
 * frequency_percent etc. into both prompts without any per-field copying.
 *
 * Before this helper existed, two divergent payload shapes (briefing carried
 * all rec fields; customer analysis carried only 4) caused the LLM to render
 * "0% of visits, 0.0-day cycle, 0 days ago" for every item in the customer
 * analysis. Using ONE function for both eliminates that drift permanently.
 */
function toLlmItemPayload(i: CustomerItem, actualQty: number = 0): Record<string, unknown> {
  return {
    ...i.rec,
    itemCode: i.itemCode,
    itemName: i.itemName ?? "",
    recommendedQty: i.recommendedQty,
    actualQty,
  };
}

interface VisitScore {
  score: number;
  coverage: number;
  accuracy: number;
}

interface AlsoBoughtEntry {
  item_code: string;
  qty: number;
}

interface InitialVisit {
  score: VisitScore;
  actualSales: Record<string, number>;
  totalActual: number;
  totalRecommended: number;
  // Server-emitted redistribution view; always present (empty groups when nothing to redistribute).
  redistributions: RedistributionView;
  alsoBought?: AlsoBoughtEntry[];
  // Saved LLM payloads (JSON); when present, modals open them instead of re-calling analytics.
  preVisitBriefing?: string | null;
  customerAnalysis?: string | null;
}

interface Props {
  sessionId: string;
  routeCode: string;
  date: string;
  customerCode: string;
  customerName: string;
  items: CustomerItem[];
  /** Server-pre-computed plan total; briefing header never re-sums. */
  totalUnits: number;
  /** True when YaumiLive shows an invoice today; parent auto-fires the visit. */
  liveVisited?: boolean;
  /** Presence is the single "visited" signal this component reads. */
  initialVisit?: InitialVisit;
  /** Briefing JSON from the auto-visit cron; flows for every planned customer.
   *  initialVisit.preVisitBriefing wins when both are set. */
  initialBriefing?: string | null;
  onRequestAnalysis: (payload: {
    sessionId: string;
    customerCode: string;
    customerName: string;
    // Same canonical shape used by the pre-visit briefing path -- every field
    // from the rec engine (Pascal/camel/snake all flow through) plus runtime
    // itemCode/itemName/recommendedQty/actualQty.
    items: Record<string, unknown>[];
    score: VisitScore;
    initialAnalysis?: string | null;
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
  totalUnits,
  liveVisited = false,
  initialVisit,
  initialBriefing: initialBriefingRaw,
  onRequestAnalysis,
}: Props) {
  const score = initialVisit?.score;
  const actuals: Record<string, number> = initialVisit?.actualSales ?? {};
  const alsoBought = initialVisit?.alsoBought ?? [];

  // Redistribution replay is heavy (~3s per route); /session/saved ships an empty
  // {groups:[]} placeholder. Fetch on drill-in unless live /visit already inlined a non-empty view.
  // Gate on groups.length>0, not truthiness, or the placeholder would suppress the fetch.
  const [redistributionView, setRedistributionView] = useState(
    initialVisit?.redistributions,
  );
  const inlineGroupsCount = initialVisit?.redistributions?.groups?.length ?? 0;
  useEffect(() => {
    if (inlineGroupsCount > 0 && initialVisit?.redistributions) {
      setRedistributionView(initialVisit.redistributions);
      return;
    }
    if (!customerCode || !routeCode || !date) return;
    let cancelled = false;
    void supervisionApi
      .getCustomerRedistribution(routeCode, date, customerCode)
      .then((r) => {
        if (!cancelled && r?.available) {
          setRedistributionView(r.redistributions as typeof redistributionView);
        }
      })
      .catch(() => {/* drill-in renders without the replay if it fails */});
    return () => { cancelled = true; };
  }, [customerCode, routeCode, date, inlineGroupsCount, initialVisit?.redistributions]);
  // Green-dot = bought something: totalActual>0 OR alsoBought non-empty.
  const visited =
    !!initialVisit &&
    ((initialVisit.totalActual ?? 0) > 0 || alsoBought.length > 0);

  // Hydrate from saved briefing JSON; visit path wins, otherwise use the briefings map.
  const initialBriefing = useMemo<Record<string, unknown> | null>(() => {
    const raw = initialVisit?.preVisitBriefing ?? initialBriefingRaw ?? null;
    if (!raw) return null;
    try {
      const parsed = JSON.parse(raw);
      return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
    } catch {
      return null;
    }
  }, [initialVisit?.preVisitBriefing, initialBriefingRaw]);

  const [briefing, setBriefing] = useState<Record<string, unknown> | null>(initialBriefing);
  const [briefingOpen, setBriefingOpen] = useState(false);
  const [briefingLoading, setBriefingLoading] = useState(false);

  // initialBriefing may arrive AFTER mount; `prev ?? initialBriefing` only fills empty.
  useEffect(() => {
    if (!initialBriefing) return;
    setBriefing((prev) => prev ?? initialBriefing);
  }, [initialBriefing]);

  const handleBriefing = async () => {
    setBriefingOpen(true);
    // Already hydrated from saved data -- nothing to fetch.
    if (briefing) return;
    setBriefingLoading(true);
    try {
      const res = await analyticsApi.preVisitBriefing({
        customer_code: customerCode,
        customer_name: customerName,
        route_code: routeCode,
        date,
        // Single canonical builder shared with the customer-analysis path.
        items: items.map((i) => toLlmItemPayload(i, actuals[i.itemCode] ?? 0)),
      });
      const payload = (res.data ?? {}) as Record<string, unknown>;
      setBriefing(payload);
      // No server-side persistence -- analyses are generated on-demand each
      // time the supervisor opens this view. The previous saveBriefing call
      // wrote to a column we no longer maintain.
    } catch {
      setBriefing({ briefing: "Briefing unavailable. Please try again.", key_items: [], heads_up: "" });
    } finally {
      setBriefingLoading(false);
    }
  };

  const handleAiClick = () => {
    onRequestAnalysis({
      sessionId,
      customerCode,
      customerName,
      // Use the same canonical builder as the briefing path so the LLM gets
      // identical per-item context (tier, frequency, cycle, days_since, why_*,
      // source, trend) for both artifacts. Before this, the customer analysis
      // dropped 8 fields, causing "0% of visits, 0.0-day cycle" hallucinations.
      items: items.map((i) => toLlmItemPayload(i, actuals[i.itemCode] ?? 0)),
      score: score ?? { score: 0, coverage: 0, accuracy: 0 },
      initialAnalysis: initialVisit?.customerAnalysis ?? null,
    });
  };

  return (
    <div className="border border-default rounded-xl bg-surface-raised overflow-hidden">
      <div className="flex items-center justify-between gap-3 px-4 py-3">
        <div className="flex-1 min-w-0 flex items-center gap-2">
          {(visited || liveVisited) && (
            <span
              className="w-2 h-2 rounded-full bg-success-500 shrink-0"
              title={
                visited
                  ? "Visit recorded -- may include off-plan items the customer bought today"
                  : "Customer invoiced today (may include off-plan items)"
              }
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
        </div>
      </div>

      <div className="border-t border-default bg-surface-sunken/40 px-4 py-4 space-y-4">
          <div className={TABLE_SCROLL_CLASS}>
          <table className="w-full text-body">
            <thead className="sticky top-0 z-10 bg-surface-sunken border-b border-default">
              <tr className="text-left text-caption font-medium text-text-tertiary uppercase tracking-wide">
                <th className="px-2 py-2 w-32">Item code</th>
                <th className="px-2 py-2">Name</th>
                <th className="px-2 py-2 w-28 text-right">Recommended</th>
                <th className="px-2 py-2 w-28 text-right">Actual (live)</th>
                <th className="px-2 py-2 w-24 text-right">Change</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-subtle">
              {items.map((it) => {
                const actual = visited ? (actuals[it.itemCode] ?? 0) : null;
                const delta = actual != null ? actual - it.recommendedQty : null;
                // Modal reads CustomerCode/Name/RouteCode/TrxDate straight off the rec.
                return (
                  <tr key={it.itemCode}>
                    <td className="px-2 py-2 font-medium text-text-primary">{it.itemCode}</td>
                    <td className="px-2 py-2 text-text-secondary">{it.itemName ?? "-"}</td>
                    <td className="px-2 py-2 text-right">
                      <RecommendedValue row={it.rec as Row} value={it.recommendedQty} />
                    </td>
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
          </div>

          {!visited && (
            <div className="space-y-2">
              <Button
                variant={briefing ? "secondary" : "primary"}
                size="sm"
                loading={briefingLoading}
                onClick={handleBriefing}
              >
                {briefing ? "Briefing read" : "Read briefing"}
              </Button>
              <p className="text-caption text-text-tertiary">
                Actuals + score appear automatically as soon as the rep
                invoices this customer.
              </p>
            </div>
          )}

          {visited && score && (
            <div className="flex items-center justify-between gap-3 flex-wrap bg-brand-50 border border-brand-100 rounded-lg px-3 py-2">
              <div className="flex items-center gap-5 text-body">
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
                  <span className="text-brand-600 ml-1 text-caption">quantity accuracy</span>
                </span>
              </div>
              <Button variant="secondary" size="sm" onClick={handleAiClick}>
                Get AI review
              </Button>
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

          {visited && redistributionView && (
            <RedistributionSection
              view={redistributionView}
              customerCode={customerCode}
              customerName={customerName}
            />
          )}
        </div>

      <Modal
        open={briefingOpen}
        onClose={() => setBriefingOpen(false)}
        title={`Pre-visit briefing - ${customerName || customerCode}`}
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
              <Badge variant="neutral">{fmtDate(date)}</Badge>
              <span className="ml-auto text-body text-text-tertiary">
                {fmtNum(items.length)} items / {fmtNum(totalUnits)} units
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
