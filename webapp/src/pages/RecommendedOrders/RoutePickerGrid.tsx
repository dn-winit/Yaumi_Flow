import { useMemo } from "react";
import Button from "@/components/ui/Button";
import RouteGrid, { type RouteStat } from "@/components/ui/RouteGrid";
import { useGenerate } from "@/hooks/useRecommendedOrder";

interface Props {
  date: string;
  routes: string[];
  routeStats?: Record<string, { customers: number; skus: number; totalQty: number }>;
  journeyCounts?: Record<string, number>;
  onRouteSelect: (routeCode: string) => void;
  onGenerated?: () => void;
}

export default function RoutePickerGrid({
  date,
  routes,
  routeStats,
  journeyCounts,
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

  // Shape order-specific stats into the generic RouteStat contract.
  // Routes with recs show stats; routes with journey but no recs show
  // "Click to generate"; routes with no journey show "No visits planned".
  const stats = useMemo<Record<string, RouteStat>>(() => {
    const out: Record<string, RouteStat> = {};
    for (const code of routes) {
      const s = routeStats?.[code];
      const jc = journeyCounts?.[code] ?? 0;

      if (s) {
        out[code] = {
          badge: { label: `${s.customers} cust`, variant: "info" },
          lines: [
            { label: "SKUs", value: s.skus.toLocaleString() },
            { label: "Total qty", value: s.totalQty.toLocaleString() },
          ],
        };
      } else if (jc > 0) {
        out[code] = {
          badge: { label: `${jc} planned`, variant: "warning" },
          lines: [{ label: "", value: "Click to generate" }],
        };
      } else {
        out[code] = {
          badge: { label: "No visits", variant: "neutral" },
          lines: [{ label: "", value: "No customers planned today" }],
        };
      }
    }
    return out;
  }, [routes, routeStats, journeyCounts]);

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
