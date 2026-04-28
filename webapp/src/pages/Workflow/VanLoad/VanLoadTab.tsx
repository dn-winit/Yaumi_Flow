import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import Card from "@/components/ui/Card";
import Button from "@/components/ui/Button";
import Loading from "@/components/ui/Loading";
import EmptyState from "@/components/ui/EmptyState";
import BarChart from "@/components/charts/BarChart";
import ContextStrip from "@/components/ui/ContextStrip";
import DatePicker from "@/components/ui/DatePicker";
import RouteGrid, { type RouteStat } from "@/components/ui/RouteGrid";
import { useFutureForecast, useForecastRouteSummary } from "@/hooks/useForecast";
import { useItemCatalog, useFilterDimensions } from "@/hooks/useDataImport";
import { useFilterOptions } from "@/hooks/useRecommendedOrder";
import { useWorkflow } from "@/pages/Workflow/workflowContext";
import VanLoadSummary from "./VanLoadSummary";
import VanLoadTable from "./VanLoadTable";
import AccuracyDrawer from "./AccuracyDrawer";
import ForecastDrawer from "./ForecastDrawer";
import ExplainabilityModal from "@/components/ui/ExplainabilityModal";
import InfoPanel from "@/components/ui/InfoPanel";
import { VAN_LOAD_INFO } from "@/config/module-info";
import { ROUTES } from "@/config/routes";
import { toNum, pickDate, fmtNum } from "@/lib/format";
import { fmtDate } from "@/lib/date";
import type { Row } from "@/types/common";
import type { FilterDimensionOption } from "@/types/data-import";

export default function VanLoadTab() {
  const navigate = useNavigate();
  // Route + date live in shared workflow context so picks carry into Visit.
  const { date, setDate, routeCode, setRouteCode } = useWorkflow();

  const [accuracyOpen, setAccuracyOpen] = useState(false);
  const [forecastOpen, setForecastOpen] = useState(false);
  const [explainRow, setExplainRow] = useState<Row | null>(null);

  // Filter dimensions (warehouse + route with parent-warehouse mapping)
  // come from data_import; journey counts come from recommended_order.
  // Both are cached at the dashboard tier so this is one pair of hits.
  const dims = useFilterDimensions();
  const filterOpts = useFilterOptions(date);

  const warehouses = dims.data?.warehouses ?? [];
  const routes = dims.data?.routes ?? [];

  // Forecast only fires once a route is chosen; the picker grid itself has
  // no per-route data dependency.
  const params = useMemo(() => {
    const p: Record<string, unknown> = {};
    if (routeCode) p.route_code = routeCode;
    return p;
  }, [routeCode]);
  const forecast = useFutureForecast(params, Boolean(routeCode));
  const catalog = useItemCatalog();

  // Price + name lookups built once from the catalog and reused across rows.
  const { priceMap, nameMap } = useMemo(() => {
    const p: Record<string, number> = {};
    const n: Record<string, string> = {};
    (catalog.data?.items ?? []).forEach((it) => {
      p[it.ItemCode] = it.last_price || it.avg_price || 0;
      n[it.ItemCode] = it.ItemName;
    });
    return { priceMap: p, nameMap: n };
  }, [catalog.data]);

  const allRows = useMemo<Row[]>(() => {
    const rows = (forecast.data?.data ?? []) as Row[];
    return rows.map((r) => {
      const code = String(r.ItemCode ?? "").trim();
      return {
        ...r,
        AvgUnitPrice: r.AvgUnitPrice ?? priceMap[code] ?? null,
        ItemName: r.ItemName ?? r.item_name ?? nameMap[code] ?? "",
      };
    });
  }, [forecast.data, priceMap, nameMap]);

  // Only positive-prediction rows count as "load this": prediction == 0
  // means the model said "skip", so showing it as a van item would be
  // misleading.
  const dateRows = useMemo(
    () =>
      allRows.filter((r) => {
        if (pickDate(r) !== date) return false;
        const p = toNum(r.prediction) ?? 0;
        return p > 0;
      }),
    [allRows, date]
  );

  const topItems = useMemo(() => {
    return [...dateRows]
      .sort((a, b) => (toNum(b.prediction) ?? 0) - (toNum(a.prediction) ?? 0))
      .slice(0, 10)
      .map((r) => ({
        ...r,
        item: String(r.ItemCode ?? "-"),
        predicted: Number((toNum(r.prediction) ?? 0).toFixed(1)),
      }));
  }, [dateRows]);

  // ---- Route picker grid (no route selected yet) ----
  if (!routeCode) {
    return (
      <VanLoadRouteGrid
        routes={routes}
        journeyCounts={filterOpts.data?.journey_counts}
        loading={dims.loading || filterOpts.loading}
        date={date}
        onSelect={setRouteCode}
      />
    );
  }

  return (
    <div className="space-y-6">
      {/* Page header carries the live scope: route is fixed once picked,
          date is editable inline (peek at any past day's van load).
          Actions ordered left-to-right: navigation back -> page drawers
          -> info help. Items count lives in the summary tile below, not
          duplicated here. */}
      <ContextStrip
        items={[
          { label: "Route", value: routeCode },
          {
            label: "Date",
            value: (
              <DatePicker value={date} onChange={setDate} className="min-w-[150px]" />
            ),
          },
        ]}
        actions={
          <>
            <Button variant="ghost" size="sm" onClick={() => setRouteCode("")}>
              ← Back to routes
            </Button>
            <Button variant="secondary" size="sm" onClick={() => setAccuracyOpen(true)}>
              Past performance
            </Button>
            <Button variant="secondary" size="sm" onClick={() => setForecastOpen(true)}>
              Upcoming plan
            </Button>
            <InfoPanel {...VAN_LOAD_INFO} />
          </>
        }
      />

      {forecast.loading ? (
        <Loading message="Loading van load..." />
      ) : dateRows.length === 0 ? (
        <Card>
          <EmptyState
            title="No forecast for this date"
            message={`Nothing forecast for route ${routeCode} on ${fmtDate(date)}.`}
            icon="📅"
            action={
              <span className="text-body text-text-tertiary">
                Run a fresh forecast from the Pipeline page.
              </span>
            }
          />
        </Card>
      ) : (
        <>
          <VanLoadSummary rows={dateRows} />

          <Card
            title="Top 10 items by van load"
            actions={
              <span className="text-body text-text-tertiary">click a bar for details</span>
            }
          >
            {topItems.length === 0 ? (
              <EmptyState title="No items to chart" icon="📊" />
            ) : (
              <BarChart
                data={topItems}
                xKey="item"
                yKey="predicted"
                height={300}
                onBarClick={(p) => setExplainRow(p as Row)}
              />
            )}
          </Card>

          <Card
            title="Van load items"
            actions={<span className="text-body text-text-tertiary">{dateRows.length} items</span>}
          >
            <VanLoadTable rows={dateRows} />
          </Card>

          {/* Forward CTA: physically advances the supervisor into the
              Visit step. The route + date scope is already in context
              so Visit lands directly in its pre-init view. */}
          <div className="flex justify-end">
            <Button variant="primary" size="md" onClick={() => navigate(ROUTES.workflowVisit)}>
              Continue to Visit →
            </Button>
          </div>
        </>
      )}

      <AccuracyDrawer
        open={accuracyOpen}
        onClose={() => setAccuracyOpen(false)}
        routeCode={routeCode}
      />
      <ForecastDrawer
        open={forecastOpen}
        onClose={() => setForecastOpen(false)}
        routeCode={routeCode}
      />
      <ExplainabilityModal
        open={explainRow != null}
        onClose={() => setExplainRow(null)}
        row={explainRow}
      />
    </div>
  );
}

/**
 * Route picker, grouped by warehouse. The grid is the picker -- no
 * separate route dropdown or warehouse filter, since each warehouse
 * already gets its own labelled section the user can scan.
 */
function VanLoadRouteGrid({
  routes,
  journeyCounts,
  loading,
  date,
  onSelect,
}: {
  routes: FilterDimensionOption[];
  journeyCounts?: Record<string, number>;
  loading: boolean;
  date: string;
  onSelect: (route: string) => void;
}) {
  const summaryQ = useForecastRouteSummary(date);

  // Build the per-route stat map once -- shared across all warehouse groups.
  const stats = useMemo<Record<string, RouteStat>>(() => {
    const out: Record<string, RouteStat> = {};
    const forecastByRoute: Record<string, { skus: number; qty: number }> = {};
    for (const r of summaryQ.data?.routes ?? []) {
      forecastByRoute[r.route_code] = { skus: r.skus, qty: r.predicted_qty };
    }
    for (const r of routes) {
      const f = forecastByRoute[r.code];
      const jc = journeyCounts?.[r.code] ?? 0;
      if (jc > 0 && f) {
        out[r.code] = {
          badge: { label: `${jc} customers`, variant: "info" },
          lines: [
            { label: "Items", value: fmtNum(f.skus) },
            { label: "Van load", value: fmtNum(f.qty) },
          ],
        };
      } else if (jc > 0) {
        out[r.code] = {
          badge: { label: `${jc} planned`, variant: "warning" },
          lines: [{ label: "", value: "No forecast yet" }],
        };
      } else {
        out[r.code] = {
          badge: { label: "No visits", variant: "neutral" },
          lines: [{ label: "", value: "No customers planned today" }],
        };
      }
    }
    return out;
  }, [routes, summaryQ.data, journeyCounts]);

  const groups = useMemo(() => {
    const byWarehouse = new Map<string, { name: string; routes: FilterDimensionOption[] }>();
    for (const r of routes) {
      const code = r.warehouse_code ?? "";
      const name = r.warehouse_name ?? "Unassigned";
      if (!byWarehouse.has(code)) byWarehouse.set(code, { name, routes: [] });
      byWarehouse.get(code)!.routes.push(r);
    }
    return Array.from(byWarehouse.entries())
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([code, g]) => ({ code, name: g.name, routes: g.routes }));
  }, [routes]);

  if (loading || summaryQ.loading) {
    return <Loading message="Loading routes..." />;
  }

  if (groups.length === 0) {
    return (
      <Card>
        <EmptyState
          title="No routes available"
          icon="🛣️"
        />
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      <p className="text-body text-text-secondary">
        Pick a route for <strong>{fmtDate(date)}</strong> to see today's van load.
      </p>
      {groups.map((g) => (
        <div key={g.code} className="space-y-2">
          <div className="flex items-baseline justify-between">
            <h3 className="text-body font-semibold text-text-primary">
              {g.name}
              <span className="ml-2 text-caption font-normal text-text-tertiary">
                {g.code}
              </span>
            </h3>
            <span className="text-caption text-text-tertiary">
              {g.routes.length} route{g.routes.length === 1 ? "" : "s"}
            </span>
          </div>
          <RouteGrid
            routes={g.routes.map((r) => r.code)}
            stats={stats}
            onSelect={onSelect}
          />
        </div>
      ))}
    </div>
  );
}
