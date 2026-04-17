import Card from "./Card";
import Badge from "./Badge";
import EmptyState from "./EmptyState";

export interface RouteStat {
  /** Small pill in the card's top-right. */
  badge?: { label: string; variant?: "info" | "success" | "warning" | "neutral" };
  /** Lines under the route code (label + value). Rendered in order. */
  lines?: { label: string; value: string }[];
}

interface RouteGridProps {
  routes: string[];
  onSelect: (routeCode: string) => void;
  /** Per-route stats; keyed by route code. Pass an empty object or omit if unknown. */
  stats?: Record<string, RouteStat>;
  /** Left-aligned summary text above the grid (e.g. "12 routes for 2026-04-14"). */
  summary?: React.ReactNode;
  /** Right-aligned slot for an action button (e.g. Regenerate). */
  actions?: React.ReactNode;
  /** Empty-state message when ``routes`` is []. */
  emptyMessage?: string;
}

/**
 * Generic clickable route grid. Purely presentational -- the caller owns the
 * data-fetching and any side-actions (regenerate, etc.). Used by both the
 * Orders tab and the Van Load tab.
 */
export default function RouteGrid({
  routes,
  onSelect,
  stats,
  summary,
  actions,
  emptyMessage = "No routes available.",
}: RouteGridProps) {
  if (routes.length === 0) {
    return (
      <Card>
        <EmptyState icon="🛣️" title="No routes available" message={emptyMessage} />
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      {(summary || actions) && (
        <div className="flex items-center justify-between gap-3">
          <div className="text-sm text-text-secondary">{summary}</div>
          {actions && <div className="flex items-center gap-3">{actions}</div>}
        </div>
      )}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
        {routes.map((code) => {
          const s = stats?.[code];
          return (
            <button
              key={code}
              onClick={() => onSelect(code)}
              className="group text-left bg-surface-raised rounded-xl shadow-1 border border-default p-6 hover:border-brand-500 hover:shadow-3 transition-all duration-base cursor-pointer"
            >
              <div className="flex items-start justify-between mb-3">
                <span className="text-2xl font-bold text-text-primary group-hover:text-brand-600 transition-colors">
                  {code}
                </span>
                {s?.badge ? (
                  <Badge variant={s.badge.variant ?? "info"}>{s.badge.label}</Badge>
                ) : (
                  <Badge variant="neutral">-</Badge>
                )}
              </div>
              {s?.lines && s.lines.length > 0 ? (
                <div className="space-y-0.5">
                  {s.lines.map((ln, i) => (
                    <p key={i} className="text-sm text-text-tertiary">
                      {ln.label}: <span className="font-medium text-text-secondary">{ln.value}</span>
                    </p>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-text-tertiary">Loading...</p>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
