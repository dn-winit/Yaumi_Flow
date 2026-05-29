import { useEffect, useMemo } from "react";
import Modal from "@/components/ui/Modal";
import Loading from "@/components/ui/Loading";
import EmptyState from "@/components/ui/EmptyState";
import Badge from "@/components/ui/Badge";
import { useAnalyzeCustomer } from "@/hooks/useAnalytics";
import AnalysisList from "./AnalysisList";

export interface CustomerAnalysisContext {
  sessionId: string;
  customerCode: string;
  customerName: string;
  routeCode: string;
  date: string;
  // Built by the canonical ``toLlmItemPayload`` helper in CustomerVisit.tsx.
  // Carries every field from the rec engine (tier, frequency, cycle,
  // days_since, why_*, source, trend) plus runtime fields. The analyzer's
  // _pick() accepts Pascal/camel/snake interchangeably.
  items: Record<string, unknown>[];
  score: { score: number; coverage: number; accuracy: number };
  // Saved analysis JSON; when present, modal skips a fresh LLM call.
  initialAnalysis?: string | null;
}

interface Props {
  open: boolean;
  onClose: () => void;
  ctx: CustomerAnalysisContext | null;
}

export default function CustomerAnalysisModal({ open, onClose, ctx }: Props) {
  const { execute, result, loading, error } = useAnalyzeCustomer();

  // Hydrate from saved JSON so re-open skips a fresh LLM call.
  const hydrated = useMemo<Record<string, unknown> | null>(() => {
    if (!ctx?.initialAnalysis) return null;
    try {
      const parsed = JSON.parse(ctx.initialAnalysis);
      return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
    } catch {
      return null;
    }
  }, [ctx?.initialAnalysis]);

  // Fire only when there's no saved review.
  useEffect(() => {
    if (!open || !ctx) return;
    if (hydrated) return;
    execute({
      customer_code: ctx.customerCode,
      route_code: ctx.routeCode,
      date: ctx.date,
      // ctx.items already in the canonical shape; analyzer reads all fields directly.
      current_items: ctx.items,
      performance_score: ctx.score.score,
      coverage: ctx.score.coverage,
      accuracy: ctx.score.accuracy,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, ctx?.customerCode, hydrated]);

  // No server persistence -- analyses are generated on-demand each time
  // the modal opens. The previous useEffect saved to a column we no longer
  // maintain; removed end-to-end. ``persistedKey`` is no longer needed.

  const a = (hydrated ?? result?.data ?? {}) as Record<string, unknown>;
  const list = (key: string): string[] => {
    const v = a[key];
    return Array.isArray(v) ? v.map(String) : [];
  };

  const title = ctx
    ? `Customer review - ${ctx.customerName || ctx.customerCode}`
    : "Customer review";

  return (
    <Modal open={open} onClose={onClose} title={title} size="xl">
      {!ctx ? (
        <EmptyState title="No customer selected" />
      ) : !hydrated && loading ? (
        <Loading message="Analyzing this visit..." />
      ) : !hydrated && error ? (
        <EmptyState icon="[!]" title="Analysis failed" message={error} />
      ) : !hydrated && !result ? (
        <Loading message="Starting analysis..." />
      ) : (
        <div className="space-y-5">
          <div className="flex flex-wrap items-center gap-3 pb-3 border-b border-subtle">
            <span className="text-body text-text-tertiary">Route</span>
            <Badge variant="info">{ctx.routeCode}</Badge>
            <span className="text-body text-text-tertiary">Date</span>
            <Badge variant="neutral">{ctx.date}</Badge>
            <span
              className="ml-auto text-body text-text-tertiary"
              title="Overall = weighted score. Items matched = share of recommended items bought. Quantity accuracy = how close actual quantities were to recommended."
            >
              Overall {ctx.score.score.toFixed(1)}% - Items matched {ctx.score.coverage.toFixed(1)}%
              - Quantity accuracy {ctx.score.accuracy.toFixed(1)}%
            </span>
          </div>

          {typeof a.performance_summary === "string" && a.performance_summary && (
            <div className="bg-surface-sunken rounded-lg border border-subtle px-4 py-3 text-body text-text-secondary leading-relaxed">
              {String(a.performance_summary)}
            </div>
          )}

          <AnalysisList title="Strengths" tone="success" items={list("strengths")} />
          <AnalysisList title="Areas for improvement" tone="warning" items={list("weaknesses")} />
          <AnalysisList
            title="Actions required"
            tone="danger"
            items={list("supervisor_instructions")}
          />
        </div>
      )}
    </Modal>
  );
}
