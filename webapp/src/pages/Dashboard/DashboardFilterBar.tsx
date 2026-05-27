import { useEffect } from "react";
import Card from "@/components/ui/Card";
import MultiSelect from "@/components/ui/MultiSelect";
import Button from "@/components/ui/Button";
import ReportingPeriodPicker from "@/components/ui/ReportingPeriodPicker";
import { useFilterDimensions } from "@/hooks/useDataImport";
import type { DashboardFilters, ReportingPeriod } from "@/types/data-import";

interface Props {
  value: DashboardFilters;
  onChange: (next: DashboardFilters) => void;
  period: ReportingPeriod;
  onPeriodChange: (next: ReportingPeriod) => void;
  /** Inclusive upper bound for the period picker (drawers pass lastActiveDate). */
  maxDate?: string;
  // Hide controls when they're redundant (e.g. drawer already scoped to one route).
  hideWarehouse?: boolean;
  hideRoute?: boolean;
}

/** Cascading Warehouse → Route → Category → Item filter strip; []="all". Auto-trims invalid picks. */
export default function DashboardFilterBar({
  value,
  onChange,
  period,
  onPeriodChange,
  maxDate,
  hideWarehouse = false,
  hideRoute = false,
}: Props) {
  const dims = useFilterDimensions(value);

  const warehouses = dims.data?.warehouses ?? [];
  const routes = dims.data?.routes ?? [];
  const categories = dims.data?.categories ?? [];
  const items = dims.data?.items ?? [];

  // Apply server-trimmed selections when they differ from the current state.
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

  // Presentation count of the user's own selections (not business data).
  const activeCount =
    value.warehouse_codes.length +
    value.route_codes.length +
    value.category_codes.length +
    value.item_codes.length;

  function update<K extends keyof DashboardFilters>(key: K, next: string[]) {
    // Cascade reset: upstream change wipes everything downstream.
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

        <ReportingPeriodPicker value={period} onChange={onPeriodChange} maxDate={maxDate} />

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

