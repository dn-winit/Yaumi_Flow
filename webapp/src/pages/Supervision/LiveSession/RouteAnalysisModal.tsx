import { useEffect } from "react";
import Modal from "@/components/ui/Modal";
import Loading from "@/components/ui/Loading";
import EmptyState from "@/components/ui/EmptyState";
import Badge from "@/components/ui/Badge";
import { useAnalyzeRoute } from "@/hooks/useAnalytics";
import AnalysisList from "./AnalysisList";

export interface RouteAnalysisContext {
  routeCode: string;
  date: string;
  visitedCustomers: Record<string, unknown>[];
  totalCustomers: number;
  totalActual: number;
  totalRecommended: number;
  actualCustomerCodes: string[];
}

interface Props {
  open: boolean;
  onClose: () => void;
  ctx: RouteAnalysisContext | null;
}

export default function RouteAnalysisModal({ open, onClose, ctx }: Props) {
  const { execute, result, loading, error } = useAnalyzeRoute();

  useEffect(() => {
    if (!open || !ctx) return;
    execute({
      route_code: ctx.routeCode,
      date: ctx.date,
      visited_customers: ctx.visitedCustomers,
      total_customers: ctx.totalCustomers,
      total_actual: ctx.totalActual,
      total_recommended: ctx.totalRecommended,
      pre_context: "",
      actual_customer_codes: ctx.actualCustomerCodes,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, ctx?.routeCode, ctx?.date]);

  const a = (result?.data ?? {}) as Record<string, unknown>;
  const list = (key: string): string[] => {
    const v = a[key];
    return Array.isArray(v) ? v.map(String) : [];
  };

  return (
    <Modal open={open} onClose={onClose} title={`Route review - ${ctx?.routeCode ?? ""}`} size="xl">
      {!ctx ? (
        <EmptyState title="No route selected" />
      ) : loading ? (
        <Loading message="Analyzing the route..." />
      ) : error ? (
        <EmptyState icon="⚠️" title="Analysis failed" message={error} />
      ) : !result ? (
        <Loading message="Starting analysis..." />
      ) : (
        <div className="space-y-5">
          <div className="flex flex-wrap items-center gap-3 pb-3 border-b border-subtle">
            <Badge variant="info">Route {ctx.routeCode}</Badge>
            <Badge variant="neutral">{ctx.date}</Badge>
            <span className="ml-auto text-sm text-text-tertiary">
              {ctx.visitedCustomers.length} / {ctx.totalCustomers} visited - Actual {ctx.totalActual} / {ctx.totalRecommended}
            </span>
          </div>

          {typeof a.route_summary === "string" && a.route_summary && (
            <div className="bg-surface-sunken rounded-lg border border-subtle px-4 py-3 text-sm text-text-secondary leading-relaxed">
              {String(a.route_summary)}
            </div>
          )}

          <AnalysisList title="Top performers" tone="success" items={list("high_performers_with_practices")} />
          <AnalysisList title="Critical issues" tone="danger" items={list("critical_issues")} />
          <AnalysisList title="Priority actions" tone="info" items={list("supervisor_priorities")} />
        </div>
      )}
    </Modal>
  );
}
