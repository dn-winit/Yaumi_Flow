import { useEffect, useState } from "react";
import Card from "@/components/ui/Card";
import Badge from "@/components/ui/Badge";
import Button from "@/components/ui/Button";
import Loading from "@/components/ui/Loading";
import CustomerGrid, { type CustomerStat } from "@/components/ui/CustomerGrid";
import { TABLE_SCROLL_CLASS } from "@/components/ui/Table";
import { useUnplannedVisits } from "@/hooks/useSupervision";
import type { UnplannedVisitor } from "@/types/supervision";
import RedistributionSection from "./RedistributionSection";

/**
 * Read-only list of customers who invoiced on the session's route/date but
 * weren't on the journey plan. Mirrors the planned-tab tile + drill-in
 * pattern so both halves of the live session feel of-a-piece. Data is
 * polled via React Query -- see ``useUnplannedVisits``.
 */
export default function UnplannedVisits({ sessionId }: { sessionId: string }) {
  const { data, loading, error, refetch, updatedAt } = useUnplannedVisits(sessionId);
  const [selectedCode, setSelectedCode] = useState<string | null>(null);

  const customers: UnplannedVisitor[] = data?.customers ?? [];

  // Server-side error: the wire payload arrived but the supervision
  // service couldn't enumerate drop-ins (bad sessionId, transient DB
  // hiccup, etc.). The contract guarantees ``error`` is a non-empty
  // string in that case and ``customers`` is empty, so we surface the
  // message visibly rather than rendering a silent empty grid.
  const serverError = data && data.success === false ? (data.error ?? "Walk-in visits unavailable.") : null;

  // Tiles for the grid. ``unique_skus``, ``total_qty`` and
  // ``live_visited`` are all server-supplied; the client just maps
  // wire field names onto component prop names (a presentation
  // adapter, no calculation).
  const tiles: CustomerStat[] = customers.map((c) => ({
    customerCode: c.customer_code,
    customerName: c.customer_name ?? "",
    uniqueSkus: c.unique_skus,
    totalUnits: c.total_qty,
    liveVisited: c.live_visited,
  }));

  const selected = selectedCode
    ? customers.find((c) => c.customer_code === selectedCode) ?? null
    : null;

  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          Walk-in visits today
          {data && <Badge variant="warning">{data.unplanned_count}</Badge>}
        </span>
      }
      actions={
        <div className="flex items-center gap-3 text-caption text-text-tertiary">
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
        <Loading message="Loading walk-in visits..." />
      ) : error ? (
        <p className="text-body text-danger-600">{error}</p>
      ) : serverError ? (
        <div
          role="alert"
          className="rounded-md border border-danger-100 bg-danger-50 px-3 py-2 text-body leading-relaxed text-danger-700"
        >
          <strong className="font-semibold">Walk-in visits unavailable:</strong>{" "}
          {serverError}
        </div>
      ) : selected ? (
        <div className="space-y-3">
          <Button variant="ghost" size="sm" onClick={() => setSelectedCode(null)}>
            <svg
              className="mr-1 inline-block h-4 w-4"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M15 19l-7-7 7-7"
              />
            </svg>
            Back to customers
          </Button>
          <div className="border border-default rounded-xl bg-surface-raised overflow-hidden">
            <div className="flex items-center justify-between gap-3 px-4 py-3">
              <div className="flex-1 min-w-0 flex items-center gap-2">
                <span
                  className="w-2 h-2 rounded-full bg-success-500 shrink-0"
                  title="Customer invoiced today (live from YaumiLive)"
                  aria-label="visited"
                />
                <div className="min-w-0">
                  <p className="text-body font-semibold text-text-primary truncate">
                    {selected.customer_name?.trim() || selected.customer_code}
                  </p>
                  <p className="text-caption text-text-tertiary">{selected.customer_code}</p>
                </div>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <Badge variant="warning">{selected.items.length} items</Badge>
                <Badge variant="info">{selected.total_qty} quantity</Badge>
              </div>
            </div>
            <div className="border-t border-default bg-surface-sunken/40 px-4 py-3 space-y-4">
              <div className={TABLE_SCROLL_CLASS}>
                <table className="w-full text-body">
                  <thead className="sticky top-0 z-10 bg-surface-sunken border-b border-default">
                    <tr className="text-left text-caption font-medium text-text-tertiary uppercase tracking-wide">
                      <th className="px-2 py-2 w-32">Item code</th>
                      <th className="px-2 py-2 text-right">Quantity sold</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-subtle">
                    {selected.items.map((it) => (
                      <tr key={it.item_code}>
                        <td className="px-2 py-2 font-medium text-text-primary">{it.item_code}</td>
                        <td className="px-2 py-2 text-right text-text-secondary">{it.qty}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <RedistributionSection
                view={selected.redistributions}
                customerCode={selected.customer_code}
                customerName={selected.customer_name ?? ""}
              />
            </div>
          </div>
        </div>
      ) : (
        <CustomerGrid
          customers={tiles}
          onSelect={setSelectedCode}
          emptyMessage="No walk-in visits on this route today - every live invoice came from a planned customer."
        />
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
