import Table from "@/components/ui/Table";
import PredictedValue from "@/components/ui/PredictedValue";
import ConfidenceBadge from "@/components/ui/ConfidenceBadge";
import type { VanLoadTableRow } from "@/api/forecast";
import type { PastPerformanceItem } from "@/types/forecast";
import type { Row } from "@/types/common";
import { fmtNum } from "@/lib/format";

interface Props {
  /** Pre-sorted rows from the page-view endpoint. The server has
   *  already substituted recommended_load -> units_to_load and the
   *  bound names -> lower_bound / upper_bound, so the table reads one
   *  canonical field per concept. No client-side sort, no fallback. */
  rows: VanLoadTableRow[];
  /** Per-(item, date) rows for the same page. Carries
   *  yaumi_total_van_load -- the rep's actual physical loading on the
   *  truck for this (route, item, date). Wired into the table by
   *  joining on item_code so we surface the rep's reality alongside our
   *  recommendation without a second fetch. ``null`` per item when no
   *  rep data exists for that day (pre-backfill / no rep activity). */
  items: PastPerformanceItem[];
  routeCode: string;
  date: string;
}

/** Adapter: wrap one page-view row plus its scope into the legacy
 *  Row shape the ExplainabilityModal expects. The modal headline reads
 *  ``prediction`` (= recommended_van_load = carry + fresh) so it
 *  matches the headline tile and the table's "On truck" column;
 *  ``units_to_load`` is also passed through so the math chain can show
 *  the fresh part separately. ``explain`` carries the business-facing
 *  diagnostics the modal renders (opening_stock,
 *  recent_avg_per_selling_day, expected_demand, forecast_below_recent,
 *  guard_skipped). */
function toLegacyRow(r: VanLoadTableRow, routeCode: string, date: string): Row {
  return {
    ItemCode: r.item_code,
    ItemName: r.item_name,
    RouteCode: routeCode,
    TrxDate: date,
    // Headline = total truck weight (carry + fresh). Matches the tile
    // and the new "On truck" table column so all three surfaces agree
    // on what "Recommended van load" means for one item.
    prediction: r.recommended_van_load,
    // Fresh-only number, surfaced separately in the modal's math chain
    // so the breakdown carry + fresh = total stays transparent.
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
  // Lookup map from item_code -> rep's actual loading numbers. Built
  // once per render; rows[] and items[] are both pre-sized by the same
  // server-side query so a missing key just means the day had no rep
  // data for that item (rendered as em-dash).
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
            <PredictedValue
              row={toLegacyRow(r, routeCode, date)}
              value={r.recommended_van_load}
            />
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
          <div className="flex flex-col" title="Rep's actual loading (yesterday's leftover + today's depot allocation)">
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
