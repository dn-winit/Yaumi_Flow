import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { recommendedOrderApi } from "@/api/recommended-order";
import type { GenerateRequest, RetrieveRequest } from "@/types/recommended-order";
import { tier } from "./refresh";

export function useOrdersSummary() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["orders-summary"],
    queryFn: () => recommendedOrderApi.getSummary(),
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useFilterOptions(date?: string) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["orders-filter-options", date ?? ""],
    queryFn: () => recommendedOrderApi.getFilterOptions(date),
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useRecommendations(params: RetrieveRequest) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["recommendations", params],
    queryFn: () => recommendedOrderApi.getRecommendations(params),
    enabled: !!params.date,
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useAdoption(
  params: { start_date: string; end_date: string; route_code?: string },
  enabled = true,
) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["orders-adoption", params],
    queryFn: () => recommendedOrderApi.getAdoption(params),
    enabled,
    ...tier("windowed"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null };
}

export function useUpcomingPlan(params: { days?: number; route_code?: string }, enabled = true) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["orders-upcoming", params],
    queryFn: () => recommendedOrderApi.getUpcoming(params),
    enabled,
    ...tier("windowed"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null };
}

export function useGenerate() {
  const qc = useQueryClient();
  const m = useMutation({
    mutationFn: (req: GenerateRequest) => recommendedOrderApi.generate(req),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["recommendations"] });
      qc.invalidateQueries({ queryKey: ["orders-summary"] });
    },
  });
  return {
    execute: (date: string, routes?: string[], force = false) =>
      m.mutateAsync({ date, route_codes: routes, force }),
    loading: m.isPending,
    error: m.error ? String(m.error) : null,
    result: m.data,
  };
}
