import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { dataImportApi } from "@/api/data-import";
import type { DashboardFilters, ReportingPeriod } from "@/types/data-import";
import { tier } from "./refresh";

// React Query key fragment for a reporting period. Stable string makes the
// cache slot deterministic across renders (same dates -> same slot).
function periodKey(p: ReportingPeriod): string {
  return `${p.start_date}::${p.end_date}`;
}

// Stable, sorted-tuple cache key for a filter combination so different
// orderings of the same selection share a query slot (mirrors the backend
// _filter_key strategy). Memoized in callers via `useMemo`-driven props,
// but cheap enough to recompute every call.
function filterKey(f?: Partial<DashboardFilters>): string {
  const part = (name: string, vals?: string[]) =>
    vals && vals.length ? `${name}=${[...vals].sort().join("|")}` : `${name}=`;
  return [
    part("w", f?.warehouse_codes),
    part("r", f?.route_codes),
    part("c", f?.category_codes),
    part("i", f?.item_codes),
  ].join(";");
}

export function useSalesOverview(period: ReportingPeriod, filters?: Partial<DashboardFilters>) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["eda-sales", periodKey(period), filterKey(filters)],
    queryFn: () => dataImportApi.getSalesOverview(period, filters),
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useBusinessKpis(period: ReportingPeriod, filters?: Partial<DashboardFilters>) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["eda-business-kpis", periodKey(period), filterKey(filters)],
    queryFn: () => dataImportApi.getBusinessKpis(period, filters),
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useFilterDimensions(filters?: Partial<DashboardFilters>, enabled = true) {
  // Pass the full selection vector (including ``item_codes``) so the
  // backend can return ``trimmed_selections`` -- the cleaned-up codes
  // the FilterBar applies when an upstream change invalidates a
  // downstream pick. ``enabled`` lets callers defer the fetch until
  // the dropdowns actually need to render.
  const selections = {
    warehouse_codes: filters?.warehouse_codes,
    route_codes: filters?.route_codes,
    category_codes: filters?.category_codes,
    item_codes: filters?.item_codes,
  };
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["eda-filter-dimensions", filterKey(selections)],
    queryFn: () => dataImportApi.getFilterDimensions(selections),
    enabled,
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

/**
 * Most recent date in sales_recent.csv. Drawers call this to seed
 * defaults that always land on a date with data. Cached at the static
 * tier because the value only changes when the data_import cron runs.
 */
export function useLastActiveDate() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["eda-last-active-date"],
    queryFn: () => dataImportApi.getLastActiveDate(),
    ...tier("static"),
  });
  return {
    date: data?.date ?? null,
    available: Boolean(data?.available),
    loading: isLoading,
    error: error ? String(error) : null,
  };
}

export function useItemCatalog(enabled = true) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["eda-items"],
    queryFn: () => dataImportApi.getItemCatalog(),
    enabled,
    ...tier("static"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useItemStats(itemCode: string | undefined, routeCode?: string) {
  const enabled = Boolean(itemCode);
  const { data, isLoading, error } = useQuery({
    queryKey: ["eda-item-stats", itemCode, routeCode ?? ""],
    queryFn: () => dataImportApi.getItemStats(itemCode as string, routeCode),
    enabled,
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null };
}

export function useDataStatus() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["data-status"],
    queryFn: () => dataImportApi.getStatus(),
    ...tier("pipeline"),
  });
  return {
    datasets: data?.datasets ?? {},
    loading: isLoading,
    error: error ? String(error) : null,
    refetch,
  };
}

export function useDataSummary() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["data-summary"],
    queryFn: () => dataImportApi.getSummary(),
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useImportDataset() {
  const qc = useQueryClient();
  const m = useMutation({
    mutationFn: ({ dataset, mode }: { dataset: string; mode: string }) =>
      dataImportApi.importDataset(dataset, mode),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["data-status"] });
      qc.invalidateQueries({ queryKey: ["data-summary"] });
    },
  });
  return {
    execute: (dataset: string, mode: string) => m.mutate({ dataset, mode }),
    loading: m.isPending,
    error: m.error ? String(m.error) : null,
    result: m.data,
  };
}

export function useImportAll() {
  const qc = useQueryClient();
  const m = useMutation({
    mutationFn: (mode: string) => dataImportApi.importAll(mode),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["data-status"] });
      qc.invalidateQueries({ queryKey: ["data-summary"] });
    },
  });
  return {
    execute: (mode: string) => m.mutate(mode),
    loading: m.isPending,
    error: m.error ? String(m.error) : null,
    result: m.data,
  };
}
