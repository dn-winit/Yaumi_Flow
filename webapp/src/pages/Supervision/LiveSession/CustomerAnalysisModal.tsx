import { useEffect, useMemo, useState } from "react";
import Modal from "@/components/ui/Modal";
import Loading from "@/components/ui/Loading";
import EmptyState from "@/components/ui/EmptyState";
import Badge from "@/components/ui/Badge";
import { useAnalyzeCustomer } from "@/hooks/useAnalytics";
import { supervisionApi } from "@/api/supervision";
import AnalysisList from "./AnalysisList";

export interface CustomerAnalysisContext {
  sessionId: string;
  customerCode: string;
  customerName: string;
  routeCode: string;
  date: string;
  items: { itemCode: string; itemName?: string; recommendedQuantity: number; actualQuantity: number }[];
  score: { score: number; coverage: number; accuracy: number };
  // Previously-saved structured analysis (JSON string). When supplied,
  // the modal renders this directly instead of re-calling the LLM.
  initialAnalysis?: string | null;
}

interface Props {
  open: boolean;
  onClose: () => void;
  ctx: CustomerAnalysisContext | null;
}

export default function CustomerAnalysisModal({ open, onClose, ctx }: Props) {
  const { execute, result, loading, error } = useAnalyzeCustomer();

  // Hydrate from a saved JSON payload when present so re-opening the
  // modal shows the same review the supervisor saw before, with no
  // second LLM call.
  const hydrated = useMemo<Record<string, unknown> | null>(() => {
    if (!ctx?.initialAnalysis) return null;
    try {
      const parsed = JSON.parse(ctx.initialAnalysis);
      return parsed && typeof parsed === "object" ? (parsed as Record<string, unknown>) : null;
    } catch {
      return null;
    }
  }, [ctx?.initialAnalysis]);

  // Tracks whether the live LLM payload has already been persisted for
  // the open ``ctx``. Prevents repeat saves when the modal re-renders.
  const [persistedKey, setPersistedKey] = useState<string | null>(null);

  // Fire the analysis when the modal opens for a customer that has no
  // saved review yet. With ``hydrated`` in scope, no LLM call needed.
  useEffect(() => {
    if (!open || !ctx) return;
    if (hydrated) return;
    execute({
      customer_code: ctx.customerCode,
      route_code: ctx.routeCode,
      date: ctx.date,
      customer_data: [],
      current_items: ctx.items,
      performance_score: ctx.score.score,
      coverage: ctx.score.coverage,
      accuracy: ctx.score.accuracy,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, ctx?.customerCode, hydrated]);

  // Persist a freshly-generated analysis once per (sessionId, customerCode)
  // so a re-open hydrates from saved storage instead of re-calling the LLM.
  useEffect(() => {
    if (!open || !ctx || !result?.data) return;
    const key = `${ctx.sessionId}::${ctx.customerCode}`;
    if (persistedKey === key) return;
    void supervisionApi
      .saveCustomerAnalysis(ctx.sessionId, ctx.customerCode, JSON.stringify(result.data))
      .catch(() => {/* fire-and-forget; logged server-side */});
    setPersistedKey(key);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, ctx?.sessionId, ctx?.customerCode, result?.data]);

  const a = (hydrated ?? result?.data ?? {}) as Record<string, unknown>;
  const list = (key: string): string[] => {
    const v = a[key];
    return Array.isArray(v) ? v.map(String) : [];
  };

  const title = ctx ? `Customer review - ${ctx.customerName || ctx.customerCode}` : "Customer review";

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
              Overall {ctx.score.score.toFixed(1)}% - Items matched {ctx.score.coverage.toFixed(1)}% - Quantity accuracy {ctx.score.accuracy.toFixed(1)}%
            </span>
          </div>

          {typeof a.performance_summary === "string" && a.performance_summary && (
            <div className="bg-surface-sunken rounded-lg border border-subtle px-4 py-3 text-body text-text-secondary leading-relaxed">
              {String(a.performance_summary)}
            </div>
          )}

          <AnalysisList title="Strengths" tone="success" items={list("strengths")} />
          <AnalysisList title="Areas for improvement" tone="warning" items={list("weaknesses")} />
          <AnalysisList title="Actions required" tone="danger" items={list("supervisor_instructions")} />
          <AnalysisList title="Likely reasons" tone="info" items={list("likely_reasons")} />
        </div>
      )}
    </Modal>
  );
}
