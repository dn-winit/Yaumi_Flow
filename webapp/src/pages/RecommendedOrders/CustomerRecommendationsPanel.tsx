import { useMemo, useState } from "react";
import Card from "@/components/ui/Card";
import Badge from "@/components/ui/Badge";
import EmptyState from "@/components/ui/EmptyState";
import RecommendedValue from "@/components/ui/RecommendedValue";
import type { RecommendationItem } from "@/types/recommended-order";
import type { Row } from "@/types/common";

interface Props {
  recommendations: RecommendationItem[];
}

interface CustomerGroup {
  customerCode: string;
  customerName: string;
  items: RecommendationItem[];
  totalQty: number;
}

export default function CustomerRecommendationsPanel({ recommendations }: Props) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const groups = useMemo<CustomerGroup[]>(() => {
    const map = new Map<string, CustomerGroup>();
    for (const rec of recommendations) {
      const existing = map.get(rec.CustomerCode);
      if (existing) {
        existing.items.push(rec);
        existing.totalQty += rec.RecommendedQuantity;
      } else {
        map.set(rec.CustomerCode, {
          customerCode: rec.CustomerCode,
          customerName: rec.CustomerName?.trim() || rec.CustomerCode,
          items: [rec],
          totalQty: rec.RecommendedQuantity,
        });
      }
    }
    return Array.from(map.values()).sort((a, b) => b.totalQty - a.totalQty);
  }, [recommendations]);

  const toggle = (code: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(code) ? next.delete(code) : next.add(code);
      return next;
    });

  if (groups.length === 0) {
    return (
      <Card title="Customers on today's route">
        <EmptyState title="No customers" icon="👥" />
      </Card>
    );
  }

  return (
    <Card
      title={`Customers on today's route — ${groups.length}`}
      actions={
        <span className="text-body text-text-tertiary">
          {recommendations.length} line items ·{" "}
          {groups.reduce((n, g) => n + g.totalQty, 0).toLocaleString()} total qty
        </span>
      }
    >
      <div className="space-y-2">
        {groups.map((g) => {
          const isOpen = expanded.has(g.customerCode);
          const sortedItems = [...g.items].sort(
            (a, b) => b.RecommendedQuantity - a.RecommendedQuantity
          );
          return (
            <div
              key={g.customerCode}
              className="border border-default rounded-lg overflow-hidden"
            >
              <button
                onClick={() => toggle(g.customerCode)}
                className="w-full flex items-center justify-between p-3 hover:bg-surface-sunken transition-colors text-left"
              >
                <div className="flex-1 min-w-0">
                  <p className="text-body font-semibold text-text-primary truncate">
                    {g.customerName}
                  </p>
                  <p className="text-caption text-text-tertiary">{g.customerCode}</p>
                </div>
                <div className="flex items-center gap-2 ml-3 shrink-0">
                  <Badge variant="neutral">{g.items.length} items</Badge>
                  <Badge variant="info">{g.totalQty.toLocaleString()} qty</Badge>
                  <svg
                    className={`h-4 w-4 text-text-tertiary transition-transform ${
                      isOpen ? "rotate-180" : ""
                    }`}
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth={2}
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M19 9l-7 7-7-7"
                    />
                  </svg>
                </div>
              </button>

              {isOpen && (
                <div className="border-t border-default bg-surface-sunken/40">
                  <table className="w-full text-body">
                    <thead>
                      <tr className="text-left text-caption font-medium text-text-tertiary uppercase tracking-wide">
                        <th className="px-4 py-2 w-32">Item code</th>
                        <th className="px-4 py-2">Item name</th>
                        <th className="px-4 py-2 w-28 text-right">Recommended</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-subtle">
                      {sortedItems.map((it, idx) => (
                        <tr key={`${it.ItemCode}-${idx}`} className="hover:bg-surface-raised">
                          <td className="px-4 py-2 font-medium text-text-primary">{it.ItemCode}</td>
                          <td className="px-4 py-2 text-text-secondary">{it.ItemName || "-"}</td>
                          <td className="px-4 py-2 text-right">
                            <RecommendedValue
                              row={it as unknown as Row}
                              value={it.RecommendedQuantity}
                            />
                          </td>
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
    </Card>
  );
}
