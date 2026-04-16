import { useEffect } from "react";
import Modal from "@/components/ui/Modal";
import Loading from "@/components/ui/Loading";
import EmptyState from "@/components/ui/EmptyState";
import Badge from "@/components/ui/Badge";
import { useAnalyzeCustomer } from "@/hooks/useAnalytics";
import AnalysisList from "./AnalysisList";

export interface CustomerAnalysisContext {
  customerCode: string;
  customerName: string;
  routeCode: string;
  date: string;
  items: { itemCode: string; itemName?: string; recommendedQuantity: number; actualQuantity: number }[];
  score: { score: number; coverage: number; accuracy: number };
}

interface Props {
  open: boolean;
  onClose: () => void;
  ctx: CustomerAnalysisContext | null;
}

export default function CustomerAnalysisModal({ open, onClose, ctx }: Props) {
  const { execute, result, loading, error } = useAnalyzeCustomer();

  // Fire the analysis when the modal opens for a new customer.
  useEffect(() => {
    if (!open || !ctx) return;
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
  }, [open, ctx?.customerCode]);

  const a = (result?.data ?? {}) as Record<string, unknown>;
  const list = (key: string): string[] => {
    const v = a[key];
    return Array.isArray(v) ? v.map(String) : [];
  };

  const title = ctx ? `Customer review - ${ctx.customerName || ctx.customerCode}` : "Customer review";

  return (
    <Modal open={open} onClose={onClose} title={title} size="xl">
      {!ctx ? (
        <EmptyState title="No customer selected" />
      ) : loading ? (
        <Loading message="Analyzing this visit..." />
      ) : error ? (
        <EmptyState icon="⚠️" title="Analysis failed" message={error} />
      ) : !result ? (
        <Loading message="Starting analysis..." />
      ) : (
        <div className="space-y-5">
          <div className="flex flex-wrap items-center gap-3 pb-3 border-b border-subtle">
            <span className="text-sm text-text-tertiary">Route</span>
            <Badge variant="info">{ctx.routeCode}</Badge>
            <span className="text-sm text-text-tertiary">Date</span>
            <Badge variant="neutral">{ctx.date}</Badge>
            <span
              className="ml-auto text-sm text-text-tertiary"
              title="Overall = weighted score. Items matched = share of recommended items bought. Qty accuracy = how close actual quantities were to recommended."
            >
              Overall {ctx.score.score.toFixed(1)}% - Items matched {ctx.score.coverage.toFixed(1)}% - Qty accuracy {ctx.score.accuracy.toFixed(1)}%
            </span>
          </div>

          {typeof a.performance_summary === "string" && a.performance_summary && (
            <div className="bg-surface-sunken rounded-lg border border-subtle px-4 py-3 text-sm text-text-secondary leading-relaxed">
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
