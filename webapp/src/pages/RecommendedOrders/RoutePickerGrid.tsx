import { useMemo } from "react";
import Button from "@/components/ui/Button";
import RouteGrid, { type RouteStat } from "@/components/ui/RouteGrid";
import { useGenerate } from "@/hooks/useRecommendedOrder";
import type { EmptyRouteDiagnosis } from "@/types/recommended-order";

interface Props {
  date: string;
  routes: string[];
  routeStats?: Record<string, { customers: number; skus: number; totalQty: number }>;
  journeyCounts?: Record<string, number>;
  routeDiagnoses?: Record<string, EmptyRouteDiagnosis>;
  onRouteSelect: (routeCode: string) => void;
  onGenerated?: () => void;
}

export default function RoutePickerGrid({
  date,
  routes,
  routeStats,
  journeyCounts,
  routeDiagnoses,
  onRouteSelect,
  onGenerated,
}: Props) {
  const { execute, loading: regenerating, error } = useGenerate();

  const handleRegenerate = async () => {
    try {
      await execute(date, undefined, true);
      onGenerated?.();
    } catch {
      /* surfaced via hook */
    }
  };

  // Shape order-specific stats into the generic RouteStat contract:
  //   * Has recs                  → customer/SKU/qty stats
  //   * Has journey but no recs   → diagnosis headline + first customer hint
  //                                 (falls back to "Click to generate" if the
  //                                  diagnosis hasn't been computed yet)
  //   * No journey                → "No customers planned today"
  const stats = useMemo<Record<string, RouteStat>>(() => {
    const out: Record<string, RouteStat> = {};
    for (const code of routes) {
      const s = routeStats?.[code];
      const jc = journeyCounts?.[code] ?? 0;
      const diag = routeDiagnoses?.[code];

      if (s) {
        out[code] = {
          badge: { label: `${s.customers} cust`, variant: "info" },
          lines: [
            { label: "SKUs", value: s.skus.toLocaleString() },
            { label: "Total qty", value: s.totalQty.toLocaleString() },
          ],
        };
      } else if (jc > 0) {
        const firstCustomer = diag?.customers?.[0];
        const customerHint = firstCustomer
          ? firstCustomer.customer_name?.trim() || firstCustomer.customer_code
          : null;
        out[code] = {
          badge: { label: `${jc} planned`, variant: "warning" },
          lines: diag
            ? [
                { label: "", value: diag.headline },
                ...(customerHint ? [{ label: "", value: customerHint }] : []),
              ]
            : [{ label: "", value: "Click to generate" }],
        };
      } else {
        out[code] = {
          badge: { label: "No visits", variant: "neutral" },
          lines: [{ label: "", value: "No customers planned today" }],
        };
      }
    }
    return out;
  }, [routes, routeStats, journeyCounts, routeDiagnoses]);

  const totals = useMemo(() => {
    const vals = Object.values(routeStats ?? {});
    if (vals.length === 0) return null;
    return {
      customers: vals.reduce((n, s) => n + s.customers, 0),
      totalQty: vals.reduce((n, s) => n + s.totalQty, 0),
    };
  }, [routeStats]);

  return (
    <RouteGrid
      routes={routes}
      stats={stats}
      onSelect={onRouteSelect}
      emptyMessage="Route list is empty. Check data_import configuration."
      summary={
        <>
          {routes.length} routes for <strong>{date}</strong>
          {totals && (
            <>
              {" · "}
              {totals.customers} customers
              {" · "}
              {totals.totalQty.toLocaleString()} total qty
            </>
          )}
        </>
      }
      actions={
        <>
          {error && <span className="text-caption text-danger-600">{error}</span>}
          <Button variant="ghost" size="sm" onClick={handleRegenerate} loading={regenerating}>
            Regenerate
          </Button>
        </>
      }
    />
  );
}
