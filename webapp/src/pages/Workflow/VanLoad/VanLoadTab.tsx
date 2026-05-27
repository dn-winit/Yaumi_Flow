import { useMemo, useState } from "react";
import Card from "@/components/ui/Card";
import Button from "@/components/ui/Button";
import { Skeleton } from "@/components/ui/Skeleton";
import EmptyState from "@/components/ui/EmptyState";
import BarChart from "@/components/charts/BarChart";
import ContextStrip from "@/components/ui/ContextStrip";
import DatePicker from "@/components/ui/DatePicker";
import RouteGrid, { type RouteStat } from "@/components/ui/RouteGrid";
import { useVanLoadPageView, useForecastRouteSummary } from "@/hooks/useForecast";
import { useFilterDimensions } from "@/hooks/useDataImport";
import { useFilterOptions } from "@/hooks/useRecommendedOrder";
import { useWorkflow, useWorkflowNavigate } from "@/pages/Workflow/workflowContext";
import VanLoadSummary from "./VanLoadSummary";
import VanLoadTable from "./VanLoadTable";
import AccuracyDrawer from "./AccuracyDrawer";
import ForecastDrawer from "./ForecastDrawer";
import ExplainabilityModal from "@/components/ui/ExplainabilityModal";
import InfoPanel from "@/components/ui/InfoPanel";
import { VAN_LOAD_INFO } from "@/config/module-info";
import { ROUTES } from "@/config/routes";
import { fmtNum } from "@/lib/format";
import { fmtDate } from "@/lib/date";
import type { Row } from "@/types/common";
import type { FilterDimensionOption } from "@/types/data-import";

export default function VanLoadTab() {
  // Workflow-aware navigate preserves ?route=...&date=... across step jumps.
  const navigate = useWorkflowNavigate();
  // Route + date live in shared workflow context so picks carry into Visit.
  const { date, setDate, routeCode, setRouteCode } = useWorkflow();

  const [accuracyOpen, setAccuracyOpen] = useState(false);
  const [forecastOpen, setForecastOpen] = useState(false);
  const [explainRow, setExplainRow] = useState<Row | null>(null);

  // Picker-only data; gated on !routeCode so a URL refresh skips two unused fetches.
  const pickerVisible = !routeCode;
  const dims = useFilterDimensions(undefined, pickerVisible);
  const filterOpts = useFilterOptions(date, pickerVisible);

  const routes = dims.data?.routes ?? [];

  // Single fetch carries summary + chart + table; server pre-aggregates so all three reconcile.
  const pageView = useVanLoadPageView(routeCode || undefined, date, Boolean(routeCode));
  const view = pageView.data;

  return !routeCode ? (
    <VanLoadRouteGrid
      routes={routes}
      journeyCounts={filterOpts.data?.journey_counts}
      loading={dims.loading || filterOpts.loading}
      date={date}
      onDateChange={setDate}
      onSelect={setRouteCode}
    />
  ) : (
    <div className="space-y-6">
      {/* Read-only route + date; "Back to routes" to change either. */}
      <ContextStrip
        items={[
          { label: "Route", value: routeCode },
          { label: "Date",  value: fmtDate(date) },
        ]}
        actions={
          <>
            <Button variant="ghost" size="sm" onClick={() => setRouteCode("")}>
              &larr; Back to routes
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

      {pageView.loading ? (
        <div className="space-y-6">
          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-4">
            {[0, 1, 2, 3].map((i) => (
              <Skeleton key={i} className="h-24" />
            ))}
          </div>
          <Skeleton className="h-72" />
          <Skeleton className="h-64" />
        </div>
      ) : !view || !view.available || view.table_rows.length === 0 ? (
        <Card>
          <EmptyState
            title="No forecast for this date"
            message={
              view?.message ||
              `Nothing forecast for route ${routeCode} on ${fmtDate(date)}.`
            }
            icon="cal"
            action={
              <span className="text-body text-text-tertiary">
                Run a fresh forecast from the Pipeline page.
              </span>
            }
          />
        </Card>
      ) : (
        <div className="space-y-6 animate-fade-in">
          <VanLoadSummary summary={view.summary} />

          <Card
            title={`Top ${view.chart_top_n.length} items by van load`}
            actions={
              <span className="text-body text-text-tertiary">click a bar for details</span>
            }
          >
            {view.chart_top_n.length === 0 ? (
              <EmptyState title="No items to chart" icon="chart" />
            ) : (
              <BarChart
                data={view.chart_top_n as unknown as Record<string, unknown>[]}
                xKey="item_code"
                yKey="predicted"
                height={300}
                onBarClick={(p) => {
                  // Look up the table row by item code -- chart and table share the same server load_df.
                  const code = String((p as { item_code?: string }).item_code ?? "");
                  const row = view.table_rows.find((r) => r.item_code === code);
                  if (!row) return;
                  setExplainRow({
                    ItemCode: row.item_code,
                    ItemName: row.item_name,
                    RouteCode: routeCode,
                    TrxDate: date,
                    // Headline = total truck weight (carry + fresh); fresh part forwarded separately.
                    prediction: row.recommended_van_load,
                    units_to_load: row.units_to_load,
                    p_demand: row.p_demand,
                    demand_class: row.demand_class,
                    class: row.demand_class,
                    lower_bound: row.lower_bound,
                    upper_bound: row.upper_bound,
                    ...row.explain,
                  } as unknown as Row);
                }}
              />
            )}
          </Card>

          <Card
            title="Van load items"
            actions={
              <span className="text-body text-text-tertiary">
                {view.table_rows.length} items
              </span>
            }
          >
            <VanLoadTable
              rows={view.table_rows}
              items={view.items}
              routeCode={routeCode}
              date={date}
            />
          </Card>

          <div className="flex justify-end">
            <Button variant="primary" size="md" onClick={() => navigate(ROUTES.workflowVisit)}>
              Continue to Visit
            </Button>
          </div>
        </div>
      )}

      <AccuracyDrawer
        open={accuracyOpen}
        onClose={() => setAccuracyOpen(false)}
        routeCode={routeCode}
        endDate={date}
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

/** Route picker grouped by warehouse; grid replaces the dropdown. */
function VanLoadRouteGrid({
  routes,
  journeyCounts,
  loading,
  date,
  onDateChange,
  onSelect,
}: {
  routes: FilterDimensionOption[];
  journeyCounts?: Record<string, number>;
  loading: boolean;
  date: string;
  onDateChange: (date: string) => void;
  onSelect: (route: string) => void;
}) {
  const summaryQ = useForecastRouteSummary(date);

  // Per-route stat map; joins server-aggregated route-summary with journey counts.
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

  const header = (
    <div className="flex flex-wrap items-center justify-between gap-4 rounded-lg border border-default bg-surface-raised px-4 py-3">
      <div className="flex items-center gap-3">
        <span className="text-caption font-semibold uppercase tracking-wider text-text-tertiary">
          Plan date
        </span>
        <DatePicker value={date} onChange={onDateChange} className="min-w-[180px]" />
      </div>
      <p className="text-body text-text-secondary">
        Showing the van load for <strong>{fmtDate(date)}</strong>. Change the
        date to see another day&apos;s plan.
      </p>
    </div>
  );

  if (loading || summaryQ.loading) {
    return (
      <div className="space-y-6">
        {header}
        {[0, 1].map((g) => (
          <div key={g} className="space-y-2">
            <Skeleton className="h-5 w-1/4" />
            <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
              {[0, 1, 2, 3].map((i) => (
                <Skeleton key={i} className="h-28" />
              ))}
            </div>
          </div>
        ))}
      </div>
    );
  }

  if (groups.length === 0) {
    return (
      <div className="space-y-6">
        {header}
        <Card>
          <EmptyState title="No routes available" icon="route" />
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {header}
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
