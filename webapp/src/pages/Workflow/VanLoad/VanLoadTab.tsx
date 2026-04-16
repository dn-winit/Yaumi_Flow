import { useMemo, useState } from "react";
import Card from "@/components/ui/Card";
import Button from "@/components/ui/Button";
import Loading from "@/components/ui/Loading";
import EmptyState from "@/components/ui/EmptyState";
import BarChart from "@/components/charts/BarChart";
import RouteGrid, { type RouteStat } from "@/components/ui/RouteGrid";
import { useFutureForecast, useForecastRouteSummary } from "@/hooks/useForecast";
import { useItemCatalog } from "@/hooks/useDataImport";
import { useFilterOptions } from "@/hooks/useRecommendedOrder";
import { useWorkflow } from "@/pages/Workflow/workflowContext";
import VanLoadFilters from "./VanLoadFilters";
import VanLoadSummary from "./VanLoadSummary";
import VanLoadTable from "./VanLoadTable";
import AccuracyDrawer from "./AccuracyDrawer";
import ForecastDrawer from "./ForecastDrawer";
import ExplainabilityModal from "@/components/ui/ExplainabilityModal";
import InfoPanel from "@/components/ui/InfoPanel";
import { VAN_LOAD_INFO } from "@/config/module-info";
import { toNum, pickDate } from "@/lib/format";
import type { Row } from "@/types/common";

export default function VanLoadTab() {
  const { date, selectedItems, setSelectedItems } = useWorkflow();
  // Route selection is tab-local: each tab independently shows the route grid
  // until the supervisor picks one here.
  const [routeCode, setRouteCodeRaw] = useState("");
  const setRouteCode = (v: string) => {
    setRouteCodeRaw(v);
    setSelectedItems([]); // changing route invalidates SKU picks
  };
  const [accuracyOpen, setAccuracyOpen] = useState(false);
  const [forecastOpen, setForecastOpen] = useState(false);
  const [explainRow, setExplainRow] = useState<Row | null>(null);

  // No `limit`: server default sized for full route horizon (single source of truth).
  const params = useMemo(() => {
    const p: Record<string, unknown> = {};
    if (routeCode) p.route_code = routeCode;
    return p;
  }, [routeCode]);

  // Only fetch the forecast once a route is chosen; the grid itself has no data deps.
  const forecast = useFutureForecast(params, Boolean(routeCode));
  const filterOpts = useFilterOptions();
  const catalog = useItemCatalog();

  // Price lookup from catalog (populated from sales_recent.csv)
  const priceMap = useMemo(() => {
    const m: Record<string, number> = {};
    (catalog.data?.items ?? []).forEach((it) => {
      m[it.ItemCode] = it.last_price || it.avg_price || 0;
    });
    return m;
  }, [catalog.data]);

  // Name lookup (fallback when forecast doesn't include item_name)
  const nameMap = useMemo(() => {
    const m: Record<string, string> = {};
    (catalog.data?.items ?? []).forEach((it) => {
      m[it.ItemCode] = it.ItemName;
    });
    return m;
  }, [catalog.data]);

  // Enrich forecast rows with price + name from catalog
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

  // Rows for the selected date
  const dateRows = useMemo(
    () => allRows.filter((r) => pickDate(r) === date),
    [allRows, date]
  );

  // SKU options derive from the entire route forecast (not date-filtered) so the
  // selection survives date changes and exposes every item the route can carry.
  const availableItems = useMemo(() => {
    const set = new Set<string>();
    allRows.forEach((r) => {
      const code = r.ItemCode;
      if (typeof code === "string" && code) set.add(code);
    });
    return Array.from(set).sort();
  }, [allRows]);

  // Apply SKU filter
  const filteredRows = useMemo(() => {
    if (selectedItems.length === 0) return dateRows;
    const set = new Set(selectedItems);
    return dateRows.filter((r) => set.has(String(r.ItemCode ?? "")));
  }, [dateRows, selectedItems]);

  const lastForecastDate = useMemo(() => {
    let latest: string | null = null;
    allRows.forEach((r) => {
      const d = pickDate(r);
      if (d && (!latest || d > latest)) latest = d;
    });
    return latest;
  }, [allRows]);

  const topItems = useMemo(() => {
    return [...filteredRows]
      .sort((a, b) => (toNum(b.prediction) ?? 0) - (toNum(a.prediction) ?? 0))
      .slice(0, 10)
      .map((r) => ({
        ...r,
        item: String(r.ItemCode ?? "-"),
        predicted: Number((toNum(r.prediction) ?? 0).toFixed(1)),
      }));
  }, [filteredRows]);

  // ---- Route grid (no route selected yet) ----
  if (!routeCode) {
    return (
      <div className="space-y-6">
        <VanLoadFilters availableItems={[]} routeCode={routeCode} setRouteCode={setRouteCode} />
        <VanLoadRouteGrid
          routes={filterOpts.data?.routes ?? []}
          loading={filterOpts.loading}
          date={date}
          onSelect={setRouteCode}
        />
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <VanLoadFilters
        availableItems={availableItems}
        routeCode={routeCode}
        setRouteCode={setRouteCode}
      />

      <div className="flex flex-wrap items-center gap-3">
        <Button variant="secondary" onClick={() => setAccuracyOpen(true)}>
          Last 30 Days Performance
        </Button>
        <Button variant="secondary" onClick={() => setForecastOpen(true)}>
          Future Forecast
        </Button>
        <div className="ml-auto">
          <InfoPanel {...VAN_LOAD_INFO} />
        </div>
      </div>

      {forecast.loading ? (
        <Loading message="Loading van load..." />
      ) : dateRows.length === 0 ? (
        <Card>
          <EmptyState
            title="No forecast for this date"
            message={`No forecast rows found for route ${routeCode} on ${date}.`}
            icon="📅"
            action={
              <span className="text-sm text-text-tertiary">
                Generate via Admin -&gt; Pipeline
              </span>
            }
          />
        </Card>
      ) : (
        <>
          <VanLoadSummary rows={filteredRows} lastForecastDate={lastForecastDate} />

          <Card
            title="Top 10 items by van load"
            actions={
              <span className="text-sm text-text-tertiary">click a bar for details</span>
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
            actions={<span className="text-sm text-text-tertiary">{filteredRows.length} items</span>}
          >
            <VanLoadTable rows={filteredRows} />
          </Card>
        </>
      )}

      <AccuracyDrawer
        open={accuracyOpen}
        onClose={() => setAccuracyOpen(false)}
        routeCode={routeCode}
        itemCodes={selectedItems}
      />
      <ForecastDrawer
        open={forecastOpen}
        onClose={() => setForecastOpen(false)}
        routeCode={routeCode}
        itemCodes={selectedItems}
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
 * Route grid shown on the VanLoad tab before a route is picked. Fetches a tiny
 * per-route aggregate (SKUs + predicted qty + peak day) for the selected date.
 */
function VanLoadRouteGrid({
  routes,
  loading,
  date,
  onSelect,
}: {
  routes: string[];
  loading: boolean;
  date: string;
  onSelect: (route: string) => void;
}) {
  const summaryQ = useForecastRouteSummary(date);
  const stats = useMemo<Record<string, RouteStat>>(() => {
    const out: Record<string, RouteStat> = {};
    for (const r of summaryQ.data?.routes ?? []) {
      out[r.route_code] = {
        badge: { label: `${r.skus} items`, variant: "info" },
        lines: [{ label: "Van load", value: r.predicted_qty.toLocaleString() }],
      };
    }
    return out;
  }, [summaryQ.data]);

  if (loading || summaryQ.loading) {
    return <Loading message="Loading routes..." />;
  }

  return (
    <RouteGrid
      routes={routes}
      stats={stats}
      onSelect={onSelect}
      summary={
        <>
          Pick a route for <strong>{date}</strong> - {routes.length} available
        </>
      }
    />
  );
}
