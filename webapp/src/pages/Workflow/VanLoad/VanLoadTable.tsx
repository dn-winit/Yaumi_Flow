import Table from "@/components/ui/Table";
import PredictedValue from "@/components/ui/PredictedValue";
import ConfidenceBadge from "@/components/ui/ConfidenceBadge";
import type { VanLoadTableRow } from "@/api/forecast";
import type { Row } from "@/types/common";

interface Props {
  /** Pre-sorted rows from the page-view endpoint. The server has
   *  already substituted recommended_load -> units_to_load and the
   *  bound names -> lower_bound / upper_bound, so the table reads one
   *  canonical field per concept. No client-side sort, no fallback. */
  rows: VanLoadTableRow[];
  routeCode: string;
  date: string;
}

/** Adapter: wrap one page-view row plus its scope into the legacy
 *  Row shape the ExplainabilityModal expects. The modal reads
 *  ``prediction``, ``demand_class``, ``lower_bound``, etc., so we map
 *  the canonical page-view fields back onto the names the modal
 *  consumes. ``explain`` carries the engine intermediates the modal
 *  renders (opening_stock, predicted_raw, forecast_corrected,
 *  bias_pct, recent_avg_per_selling_day, expected_demand,
 *  pattern_floor_applied, pattern_ceiling_applied,
 *  forecast_below_recent, guard_skipped). */
function toLegacyRow(r: VanLoadTableRow, routeCode: string, date: string): Row {
  return {
    ItemCode: r.item_code,
    ItemName: r.item_name,
    RouteCode: routeCode,
    TrxDate: date,
    prediction: r.units_to_load,
    p_demand: r.p_demand,
    demand_class: r.demand_class,
    class: r.demand_class,
    lower_bound: r.lower_bound,
    upper_bound: r.upper_bound,
    ...r.explain,
  } as unknown as Row;
}

export default function VanLoadTable({ rows, routeCode, date }: Props) {
  const columns = [
    {
      key: "ItemCode",
      label: "Item Code",
      render: (r: VanLoadTableRow) => (
        <span className="font-medium text-text-primary">{r.item_code}</span>
      ),
    },
    {
      key: "ItemName",
      label: "Item Name",
      render: (r: VanLoadTableRow) => r.item_name || "-",
    },
    {
      key: "Predicted",
      label: "Units to load",
      render: (r: VanLoadTableRow) => (
        <PredictedValue
          row={toLegacyRow(r, routeCode, date)}
          value={r.units_to_load}
        />
      ),
    },
    {
      key: "Confidence",
      label: "Chance of selling",
      render: (r: VanLoadTableRow) => (
        <ConfidenceBadge
          value={r.p_demand}
          demandClass={r.demand_class ?? undefined}
        />
      ),
    },
    {
      key: "Range",
      label: "Likely range (low-high)",
      render: (r: VanLoadTableRow) => {
        if (r.lower_bound == null || r.upper_bound == null) return "-";
        return `${r.lower_bound.toFixed(1)} - ${r.upper_bound.toFixed(1)}`;
      },
    },
  ];

  return (
    <Table
      data={rows as unknown as Record<string, unknown>[]}
      columns={columns as unknown as Parameters<typeof Table>[0]["columns"]}
      emptyMessage="No items to load"
    />
  );
}
