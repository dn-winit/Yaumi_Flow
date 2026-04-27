import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { dataImportApi } from "@/api/data-import";
import type { DashboardFilters } from "@/types/data-import";
import { tier } from "./refresh";

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

export function useSalesOverview(lookback?: string, filters?: Partial<DashboardFilters>) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["eda-sales", lookback ?? "default", filterKey(filters)],
    queryFn: () => dataImportApi.getSalesOverview(lookback, filters),
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useBusinessKpis(lookback?: string, filters?: Partial<DashboardFilters>) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["eda-business-kpis", lookback ?? "default", filterKey(filters)],
    queryFn: () => dataImportApi.getBusinessKpis(lookback, filters),
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useForecastRows(
  lookback?: string,
  filters?: Partial<DashboardFilters>,
  enabled = true,
) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["eda-forecast-rows", lookback ?? "default", filterKey(filters)],
    queryFn: () => dataImportApi.getForecastRows(lookback, filters),
    enabled,
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useFilterDimensions(filters?: Partial<DashboardFilters>) {
  // Item selections don't shrink the upstream dropdowns, so we exclude
  // them from the cache key to avoid pointless refetches.
  const upstream = {
    warehouse_codes: filters?.warehouse_codes,
    route_codes: filters?.route_codes,
    category_codes: filters?.category_codes,
  };
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["eda-filter-dimensions", filterKey(upstream)],
    queryFn: () => dataImportApi.getFilterDimensions(upstream),
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useItemCatalog() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["eda-items"],
    queryFn: () => dataImportApi.getItemCatalog(),
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
