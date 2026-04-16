import { useMutation, useQuery } from "@tanstack/react-query";
import { analyticsApi } from "@/api/analytics";

export function useAnalyzeCustomer() {
  const m = useMutation({
    mutationFn: (data: Record<string, unknown>) => analyticsApi.analyzeCustomer(data),
  });
  return {
    execute: (data: Record<string, unknown>) => m.mutate(data),
    loading: m.isPending,
    error: m.error ? String(m.error) : null,
    result: m.data,
  };
}

export function useAnalyzeRoute() {
  const m = useMutation({
    mutationFn: (data: Record<string, unknown>) => analyticsApi.analyzeRoute(data),
  });
  return {
    execute: (data: Record<string, unknown>) => m.mutate(data),
    loading: m.isPending,
    error: m.error ? String(m.error) : null,
    result: m.data,
  };
}

export function useCacheStats() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["cache-stats"],
    queryFn: () => analyticsApi.getCacheStats(),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}
