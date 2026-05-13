import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { forecastApi } from "@/api/forecast";
import { tier } from "./refresh";

export function useForecastSummary() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["forecast-summary"],
    queryFn: () => forecastApi.getSummary(),
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useForecastRouteSummary(date?: string, enabled = true) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["forecast-route-summary", date ?? ""],
    queryFn: () => forecastApi.getForecastRouteSummary(date),
    enabled,
    ...tier("windowed"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null };
}

/**
 * VanLoad route-detail page-view: one fetch carries the summary tiles,
 * the top-N chart, and the table -- all pre-computed, pre-sorted, with
 * one canonical field per concept. The page binds directly; no client-
 * side aggregation, sort, or field substitution.
 */
export function useVanLoadPageView(
  routeCode: string | undefined,
  date: string | undefined,
  enabled = true,
  topN: number = 10,
) {
  const ready = enabled && Boolean(routeCode) && Boolean(date);
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["van-load-page-view", routeCode ?? "", date ?? "", topN],
    queryFn: () => forecastApi.getVanLoadPageView(routeCode!, date!, topN),
    enabled: ready,
    ...tier("windowed"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

/**
 * Upcoming-plan drawer page-view: one fetch carries the horizon tiles,
 * the daily chart series (with band on single-SKU views), and the
 * line-item table. Server picks ``show_band`` and rounds wire bytes to
 * match the route-summary contract; the drawer binds and renders.
 */
export function useForecastDrawerPageView(
  routeCode: string | undefined,
  itemCodes: string[] | undefined,
  fromDate: string | undefined,
  enabled = true,
) {
  const ready = enabled && Boolean(routeCode) && Boolean(fromDate);
  const itemKey = (itemCodes ?? []).slice().sort().join("|");
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: [
      "forecast-drawer-page-view",
      routeCode ?? "",
      fromDate ?? "",
      itemKey,
    ],
    queryFn: () =>
      forecastApi.getForecastDrawerPageView(routeCode, itemCodes, fromDate),
    enabled: ready,
    ...tier("windowed"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

/**
 * Resolved Pipeline page payload -- one fetch with per-step status,
 * formatted metric / detail strings, cascade summary, and the global
 * ``any_running`` flag pre-computed server-side. The page renders the
 * response verbatim; no client-side resolution.
 */
export function useResolvedPipelineStatus() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["pipeline-resolved-status"],
    queryFn: () => forecastApi.getResolvedPipelineStatus(),
    ...tier("pipeline"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

/* ---- Auto-retrain ---- */

export function useRetrainConfig() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["retrain-config"],
    queryFn: () => forecastApi.getRetrainConfig(),
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useRetrainHistory() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["retrain-history"],
    queryFn: () => forecastApi.getRetrainHistory(),
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useUpdateRetrainConfig() {
  const qc = useQueryClient();
  const mutation = useMutation({
    mutationFn: (updates: Parameters<typeof forecastApi.updateRetrainConfig>[0]) =>
      forecastApi.updateRetrainConfig(updates),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["retrain-config"] });
      qc.invalidateQueries({ queryKey: ["retrain-history"] });
    },
  });
  return mutation;
}

/**
 * Per-day series + return metrics for the AccuracyDrawer's past-performance
 * chart. Three plottable lines (original van load, reconciled load, actually
 * sold) for the given route over the ``[start_date, end_date]`` window.
 */
export function useReconciliationPastPerformance(
  routeCode: string | undefined,
  startDate: string | undefined,
  endDate: string | undefined,
  enabled = true,
  filters?: { item_codes?: string[]; category_codes?: string[] },
) {
  const ready = enabled && Boolean(routeCode) && Boolean(startDate) && Boolean(endDate);
  const itemKey = (filters?.item_codes ?? []).slice().sort().join("|");
  const catKey = (filters?.category_codes ?? []).slice().sort().join("|");
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: [
      "reconciliation-past-performance",
      routeCode ?? "",
      startDate ?? "",
      endDate ?? "",
      itemKey,
      catKey,
    ],
    queryFn: () =>
      forecastApi.getReconciliationPastPerformance(routeCode!, startDate!, endDate!, filters),
    enabled: ready,
    ...tier("windowed"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useTriggerPipeline() {
  const qc = useQueryClient();
  // Trigger mutations invalidate the resolved-status query (which the
  // Pipeline page binds to) so the strip refreshes the moment a run
  // starts. There is no longer a raw ``pipeline-status`` consumer.
  const onSuccess = () =>
    qc.invalidateQueries({ queryKey: ["pipeline-resolved-status"] });
  const train = useMutation({
    mutationFn: () => forecastApi.triggerTraining(),
    onSuccess,
  });
  const inference = useMutation({
    mutationFn: () => forecastApi.triggerInference(),
    onSuccess,
  });
  // ``mutateAsync`` so callers can await the response and inspect
  // ``success``/``message`` -- the server returns 200 with
  // ``{success: false}`` for guard / preflight failures, so the toast
  // wording must follow the payload, not the HTTP status.
  return {
    triggerTrain: () => train.mutateAsync(),
    triggerInference: () => inference.mutateAsync(),
    loading: train.isPending || inference.isPending,
    error: train.error || inference.error ? String(train.error || inference.error) : null,
  };
}
