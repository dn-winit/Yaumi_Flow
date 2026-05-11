import { useEffect } from "react";
import Card from "@/components/ui/Card";
import MultiSelect from "@/components/ui/MultiSelect";
import Select from "@/components/ui/Select";
import Button from "@/components/ui/Button";
import { useFilterDimensions } from "@/hooks/useDataImport";
import { LOOKBACK_OPTIONS, type Lookback } from "@/lib/format";
import type { DashboardFilters } from "@/types/data-import";

interface Props {
  value: DashboardFilters;
  onChange: (next: DashboardFilters) => void;
  lookback: Lookback;
  onLookbackChange: (next: Lookback) => void;
  // Hide controls that are redundant in the calling context (e.g. the
  // VanLoad "Past analysis" drawer is already scoped to one route, so
  // showing Warehouse + Route again would just clutter the strip).
  hideWarehouse?: boolean;
  hideRoute?: boolean;
}

/**
 * Cascading multi-select filter strip for the Dashboard. Order is enforced
 * left-to-right (Warehouse → Route → Category → Item); each downstream
 * dropdown only shows options compatible with every upstream pick.
 *
 * Out-of-range selections are auto-trimmed when an upstream change makes
 * them invalid -- the user never has to manually clean up after themselves.
 *
 * Empty selection === "all" by convention (matches backend semantics).
 */
export default function DashboardFilterBar({
  value,
  onChange,
  lookback,
  onLookbackChange,
  hideWarehouse = false,
  hideRoute = false,
}: Props) {
  const dims = useFilterDimensions(value);

  const warehouses = dims.data?.warehouses ?? [];
  const routes = dims.data?.routes ?? [];
  const categories = dims.data?.categories ?? [];
  const items = dims.data?.items ?? [];

  // Auto-trim invalid selections by reading ``trimmed_selections`` from
  // the backend response. Server already validated each code against
  // the cascaded option sets -- the client just applies the cleaned
  // vector when it differs from the current state.
  useEffect(() => {
    if (dims.loading || !dims.data) return;
    const trimmed = dims.data.trimmed_selections;
    if (!trimmed) return;
    if (
      trimmed.warehouse_codes.length !== value.warehouse_codes.length ||
      trimmed.route_codes.length !== value.route_codes.length ||
      trimmed.category_codes.length !== value.category_codes.length ||
      trimmed.item_codes.length !== value.item_codes.length
    ) {
      onChange(trimmed);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dims.data, dims.loading]);

  // The "N filters active" badge is a presentation count of the user's
  // own UI state (length of each selection array). Not a calculation
  // on business data -- the underlying values are what the user just
  // picked in the dropdowns.
  const activeCount =
    value.warehouse_codes.length +
    value.route_codes.length +
    value.category_codes.length +
    value.item_codes.length;

  function update<K extends keyof DashboardFilters>(key: K, next: string[]) {
    // Cascade reset: changing an upstream level clears every downstream
    // selection so the user starts fresh in the new scope. The auto-trim
    // effect above is a safety net; this is the primary intent.
    const out: DashboardFilters = { ...value, [key]: next };
    if (key === "warehouse_codes") {
      out.route_codes = [];
      out.category_codes = [];
      out.item_codes = [];
    } else if (key === "route_codes") {
      out.category_codes = [];
      out.item_codes = [];
    } else if (key === "category_codes") {
      out.item_codes = [];
    }
    onChange(out);
  }

  function reset() {
    onChange({
      warehouse_codes: [],
      route_codes: [],
      category_codes: [],
      item_codes: [],
    });
  }

  return (
    <Card>
      <div className="flex flex-wrap items-end gap-3">
        {!hideWarehouse && (
          <MultiSelect
            label="Warehouse"
            options={warehouses}
            value={value.warehouse_codes}
            onChange={(v) => update("warehouse_codes", v)}
            loading={dims.loading && warehouses.length === 0}
          />
        )}
        {!hideRoute && (
          <MultiSelect
            label="Route"
            options={routes}
            value={value.route_codes}
            onChange={(v) => update("route_codes", v)}
            loading={dims.loading}
          />
        )}
        <MultiSelect
          label="Category"
          options={categories}
          value={value.category_codes}
          onChange={(v) => update("category_codes", v)}
          loading={dims.loading}
        />
        <MultiSelect
          label="Item"
          options={items}
          value={value.item_codes}
          onChange={(v) => update("item_codes", v)}
          loading={dims.loading}
        />

        <Select
          label="Reporting period"
          value={lookback}
          onChange={(v) => onLookbackChange(v as Lookback)}
          options={LOOKBACK_OPTIONS.map((o) => ({ value: o.key, label: o.label }))}
          className="min-w-[220px]"
        />

        <div className="ml-auto flex items-center gap-2">
          {activeCount > 0 && (
            <span className="text-caption text-text-tertiary">
              {activeCount} filter{activeCount === 1 ? "" : "s"} active
            </span>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={reset}
            disabled={activeCount === 0}
          >
            Reset
          </Button>
        </div>
      </div>
    </Card>
  );
}

