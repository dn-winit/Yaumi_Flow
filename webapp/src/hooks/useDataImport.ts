import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { dataImportApi } from "@/api/data-import";
import { tier } from "./refresh";

export function useSalesOverview() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["eda-sales"],
    queryFn: () => dataImportApi.getSalesOverview(),
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useBusinessKpis() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["eda-business-kpis"],
    queryFn: () => dataImportApi.getBusinessKpis(),
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

export function useCustomerOverview(lookbackDays = 90) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["eda-customers", lookbackDays],
    queryFn: () => dataImportApi.getCustomerOverview(lookbackDays),
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
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
