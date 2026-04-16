import { useEffect, useState } from "react";
import Card from "@/components/ui/Card";
import Badge from "@/components/ui/Badge";
import Loading from "@/components/ui/Loading";
import { useUnplannedVisits } from "@/hooks/useSupervision";
import type { UnplannedVisitor } from "@/types/supervision";

/**
 * Read-only list of customers who invoiced on the session's route/date but
 * weren't on the journey plan. Data is polled via React Query -- see the
 * ``useUnplannedVisits`` hook for cadence and caching.
 */
export default function UnplannedVisits({ sessionId }: { sessionId: string }) {
  const { data, loading, error, refetch, updatedAt } = useUnplannedVisits(sessionId);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const toggle = (code: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(code) ? next.delete(code) : next.add(code);
      return next;
    });

  const customers: UnplannedVisitor[] = data?.customers ?? [];

  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          Unplanned visits today
          {data && <Badge variant="warning">{data.unplanned_count}</Badge>}
        </span>
      }
      actions={
        <div className="flex items-center gap-3 text-xs text-text-tertiary">
          <FreshnessLabel updatedAt={updatedAt} />
          <button
            onClick={() => refetch()}
            disabled={loading}
            className="text-body text-brand-600 hover:text-brand-700 font-medium disabled:opacity-50"
          >
            {loading ? "Refreshing..." : "Refresh"}
          </button>
        </div>
      }
    >
      {loading && !data ? (
        <Loading message="Loading unplanned visits..." />
      ) : error ? (
        <p className="text-body text-danger-600">{error}</p>
      ) : customers.length === 0 ? (
        <p className="text-sm text-text-tertiary">
          No drop-in visits on this route today &mdash; every live invoice came from a planned customer.
        </p>
      ) : (
        <div className="space-y-2">
          {customers.map((c) => {
            const isOpen = expanded.has(c.customer_code);
            return (
              <div
                key={c.customer_code}
                className="border border-default rounded-xl bg-surface-raised overflow-hidden"
              >
                <button
                  onClick={() => toggle(c.customer_code)}
                  className="w-full flex items-center justify-between gap-3 px-4 py-3 hover:bg-surface-sunken transition-colors text-left"
                >
                  <div className="flex-1 min-w-0 flex items-center gap-2">
                    <span
                      className="w-2 h-2 rounded-full bg-success-500 shrink-0"
                      title="Customer invoiced today (live from YaumiLive)"
                      aria-label="visited"
                    />
                    <div className="min-w-0">
                      <p className="text-sm font-semibold text-text-primary truncate">
                        {c.customer_name?.trim() || c.customer_code}
                      </p>
                      <p className="text-xs text-text-tertiary">{c.customer_code}</p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <Badge variant="warning">{c.items.length} items</Badge>
                    <Badge variant="info">{c.total_qty} qty</Badge>
                    <svg
                      className={`w-4 h-4 text-text-tertiary transition-transform ${isOpen ? "rotate-180" : ""}`}
                      fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
                    </svg>
                  </div>
                </button>

                {isOpen && (
                  <div className="border-t border-default bg-surface-sunken/40 px-4 py-3">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="text-left text-xs font-medium text-text-tertiary uppercase tracking-wide">
                          <th className="px-2 py-2 w-32">Item code</th>
                          <th className="px-2 py-2 text-right">Qty sold</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-subtle">
                        {c.items.map((it) => (
                          <tr key={it.item_code}>
                            <td className="px-2 py-2 font-medium text-text-primary">{it.item_code}</td>
                            <td className="px-2 py-2 text-right text-text-secondary">{it.qty}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </Card>
  );
}

function FreshnessLabel({ updatedAt }: { updatedAt: number | undefined }) {
  // Small local tick (no DB hit) so the label counts up in real time between polls.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 5_000);
    return () => window.clearInterval(id);
  }, []);
  if (!updatedAt) return null;
  const secs = Math.max(0, Math.round((now - updatedAt) / 1000));
  return <span>Updated {formatAgo(secs)} ago</span>;
}

function formatAgo(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const mins = Math.floor(seconds / 60);
  if (mins < 60) return `${mins}m`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h`;
}
