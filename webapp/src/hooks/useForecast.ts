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

/** VanLoad page-view -- summary tiles + top-N chart + table in one fetch (page binds verbatim). */
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

/** Upcoming-plan drawer page-view -- horizon tiles + daily chart + line items in one fetch. */
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

/** Pipeline page payload -- per-step status, cascade summary, any_running in one fetch. */
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

/** AccuracyDrawer past-performance: three daily series over [start_date, end_date]. */
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

// Module-scoped so a rapid re-trigger (Retrain, then Generate Forecasts) cleanly
// tears down the prior 30-min watcher before arming a new one.
let _postRunWatcherHandle: number | null = null;

export function useTriggerPipeline() {
  const qc = useQueryClient();
  // Refresh the Pipeline strip the moment a run starts.
  const onTriggerSuccess = () =>
    qc.invalidateQueries({ queryKey: ["pipeline-resolved-status"] });

  // After the run completes, invalidate downstream queries that read freshly-written
  // artifacts (accuracy tiles, history, config) so dashboard lag drops from ~5min to ~0.
  const invalidatePostRun = () => {
    qc.invalidateQueries({ queryKey: ["forecast-summary"] });
    qc.invalidateQueries({ queryKey: ["retrain-history"] });
    qc.invalidateQueries({ queryKey: ["retrain-config"] });
  };

  const armPostRunInvalidation = () => {
    // Tear down any prior watcher first; back-to-back triggers must not leak intervals.
    if (_postRunWatcherHandle !== null) {
      window.clearInterval(_postRunWatcherHandle);
      _postRunWatcherHandle = null;
    }

    // Fire only after any_running has been stably false for STABLE_FALSE_MS.
    // Required because auto_inference_after_train flashes false between train+inference;
    // 6s (two polls) is longer than that gap so we don't fire mid-chain.
    let wasRunning = false;
    let stableFalseSince: number | null = null;
    const STABLE_FALSE_MS = 6_000;
    const start = Date.now();
    const handle = window.setInterval(() => {
      const data = qc.getQueryData<{ any_running?: boolean }>([
        "pipeline-resolved-status",
      ]);
      const running = Boolean(data?.any_running);

      if (running) {
        wasRunning = true;
        // Reset debounce -- the prior "false" was the gap between train and inference.
        stableFalseSince = null;
      } else if (wasRunning) {
        if (stableFalseSince === null) {
          stableFalseSince = Date.now();
        } else if (Date.now() - stableFalseSince >= STABLE_FALSE_MS) {
          // Stably idle -- auto-chain done. Invalidate downstream.
          invalidatePostRun();
          window.clearInterval(handle);
          if (_postRunWatcherHandle === handle) {
            _postRunWatcherHandle = null;
          }
        }
      }
      // Safety teardown after 30 minutes of polling.
      if (Date.now() - start > 30 * 60 * 1000) {
        window.clearInterval(handle);
        if (_postRunWatcherHandle === handle) {
          _postRunWatcherHandle = null;
        }
      }
    }, 3_000);
    _postRunWatcherHandle = handle;
  };

  const onSuccess = () => {
    onTriggerSuccess();
    armPostRunInvalidation();
  };

  const train = useMutation({
    mutationFn: () => forecastApi.triggerTraining(),
    onSuccess,
  });
  const inference = useMutation({
    mutationFn: () => forecastApi.triggerInference(),
    onSuccess,
  });
  // mutateAsync so callers can read {success, message} -- the server returns
  // 200 with {success:false} for guard failures, so toasts follow payload, not HTTP.
  return {
    triggerTrain: () => train.mutateAsync(),
    triggerInference: () => inference.mutateAsync(),
    loading: train.isPending || inference.isPending,
    error: train.error || inference.error ? String(train.error || inference.error) : null,
  };
}
