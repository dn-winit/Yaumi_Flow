import Card from "./Card";
import EmptyState from "./EmptyState";
import { fmtNum } from "@/lib/format";

export interface CustomerStat {
  customerCode: string;
  customerName: string;
  uniqueSkus: number;
  totalUnits: number;
  /** True if a visit was processed for this customer in the current session. */
  visited?: boolean;
  /** True if YaumiLive shows an invoice today, even with no in-session visit. */
  liveVisited?: boolean;
}

interface Props {
  customers: CustomerStat[];
  onSelect: (customerCode: string) => void;
  /** Left-aligned summary text above the grid. */
  summary?: React.ReactNode;
  /** Right-aligned slot for action buttons. */
  actions?: React.ReactNode;
  emptyMessage?: string;
}

/** Clickable customer grid for the live session; mirrors RouteGrid's tile language. */
export default function CustomerGrid({
  customers,
  onSelect,
  summary,
  actions,
  emptyMessage = "No customers planned for this route.",
}: Props) {
  if (customers.length === 0) {
    return (
      <Card>
        <EmptyState icon="👥" title="No customers" message={emptyMessage} />
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      {(summary || actions) && (
        <div className="flex items-center justify-between gap-3">
          <div className="text-body text-text-secondary">{summary}</div>
          {actions && <div className="flex items-center gap-3">{actions}</div>}
        </div>
      )}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {customers.map((c) => (
          <button
            key={c.customerCode}
            onClick={() => onSelect(c.customerCode)}
            className="group text-left bg-surface-raised rounded-lg border-l-3 border-brand-100 shadow-1 p-5 hover:border-brand-500 hover:shadow-2 hover:-translate-y-0.5 transition-all duration-base cursor-pointer"
          >
            <div className="mb-3 min-w-0">
              <div className="flex items-center gap-2">
                {(c.visited || c.liveVisited) && (
                  <span
                    className="w-2 h-2 rounded-full bg-success-500 shrink-0"
                    title={
                      c.visited
                        ? "Visit recorded -- may include off-plan items the customer bought today"
                        : "Customer invoiced today (may include off-plan items)"
                    }
                    aria-label="visited"
                  />
                )}
                <p className="text-body font-bold text-text-primary group-hover:text-brand-600 transition-colors truncate">
                  {c.customerCode}
                </p>
              </div>
              <p className="text-caption text-text-secondary truncate">
                {c.customerName.trim() || "—"}
              </p>
            </div>
            <div className="space-y-0.5">
              <p className="text-body text-text-tertiary">
                Different items:{" "}
                <span className="font-medium text-text-secondary">
                  {fmtNum(c.uniqueSkus)}
                </span>
              </p>
              <p className="text-body text-text-tertiary">
                Units to deliver:{" "}
                <span className="font-medium text-text-secondary">
                  {fmtNum(c.totalUnits)}
                </span>
              </p>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}
