import Table from "@/components/ui/Table";
import PredictedValue from "@/components/ui/PredictedValue";
import ConfidenceBadge from "@/components/ui/ConfidenceBadge";
import type { VanLoadTableRow } from "@/api/forecast";
import type { PastPerformanceItem } from "@/types/forecast";
import type { Row } from "@/types/common";
import { fmtNum } from "@/lib/format";

interface Props {
  /** Pre-sorted page-view rows; no client-side sort/fallback. */
  rows: VanLoadTableRow[];
  /** Per-(item, date) rep loading joined by item_code; null when no rep data for that day. */
  items: PastPerformanceItem[];
  routeCode: string;
  date: string;
}

/** Adapter to the legacy Row shape ExplainabilityModal expects;
 *  prediction = total truck weight, units_to_load = fresh part, explain = diagnostics. */
function toLegacyRow(r: VanLoadTableRow, routeCode: string, date: string): Row {
  return {
    ItemCode: r.item_code,
    ItemName: r.item_name,
    RouteCode: routeCode,
    TrxDate: date,
    // Total truck weight (carry + fresh); matches the tile + "On truck" column.
    prediction: r.recommended_van_load,
    // Fresh-only; surfaced separately so the modal's math chain stays transparent.
    units_to_load: r.units_to_load,
    p_demand: r.p_demand,
    demand_class: r.demand_class,
    class: r.demand_class,
    lower_bound: r.lower_bound,
    upper_bound: r.upper_bound,
    ...r.explain,
  } as unknown as Row;
}

export default function VanLoadTable({ rows, items, routeCode, date }: Props) {
  // item_code -> rep loading lookup; missing key -> em-dash (no rep data that day).
  const yaumiByItem = new Map<string, PastPerformanceItem>();
  for (const it of items) yaumiByItem.set(it.itemCode, it);

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
      key: "OnTruck",
      label: "On truck (carry + fresh)",
      render: (r: VanLoadTableRow) => {
        const carriedRaw = r.explain?.opening_stock;
        const carried = typeof carriedRaw === "number" ? carriedRaw : null;
        const fresh = r.units_to_load;
        return (
          <div className="flex flex-col">
            <PredictedValue row={toLegacyRow(r, routeCode, date)} value={r.recommended_van_load} />
            <span className="text-caption text-text-tertiary tabular-nums">
              {carried != null ? fmtNum(carried) : "—"} carried + {fmtNum(fresh)} fresh
            </span>
          </div>
        );
      },
    },
    {
      key: "RepLoading",
      label: "Rep's actual loading",
      render: (r: VanLoadTableRow) => {
        const yaumi = yaumiByItem.get(r.item_code);
        const total = yaumi?.yaumi_total_van_load;
        if (!yaumi || total == null) {
          return (
            <span
              className="text-text-tertiary"
              title="No rep loading data recorded for this item on this date"
            >
              -
            </span>
          );
        }
        const open = yaumi.yaumi_opening_stock;
        const fresh = yaumi.yaumi_fresh_load;
        return (
          <div
            className="flex flex-col"
            title="Rep's actual loading (yesterday's leftover + today's depot allocation)"
          >
            <span className="tabular-nums text-text-primary">{fmtNum(total)}</span>
            {open != null && fresh != null && (
              <span className="text-caption text-text-tertiary tabular-nums">
                {fmtNum(open)} carried + {fmtNum(fresh)} fresh
              </span>
            )}
          </div>
        );
      },
    },
    {
      key: "Confidence",
      label: "Chance of selling",
      render: (r: VanLoadTableRow) => (
        <ConfidenceBadge value={r.p_demand} demandClass={r.demand_class ?? undefined} />
      ),
    },
    {
      key: "Range",
      label: "Likely range (low-high)",
      render: (r: VanLoadTableRow) => {
        if (r.lower_bound == null || r.upper_bound == null) return "-";
        return `${fmtNum(r.lower_bound)} - ${fmtNum(r.upper_bound)}`;
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
