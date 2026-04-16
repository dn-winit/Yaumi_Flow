import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { forecastApi, type AccuracyParams } from "@/api/forecast";
import { tier } from "./refresh";

export function useAccuracyComparison(params: AccuracyParams, enabled = true) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["accuracy-comparison", params],
    queryFn: () => forecastApi.getAccuracyComparison(params),
    enabled,
    ...tier("windowed"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useForecastSummary() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["forecast-summary"],
    queryFn: () => forecastApi.getSummary(),
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useTestPredictions(params?: Record<string, unknown>) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["test-predictions", params ?? {}],
    queryFn: () => forecastApi.getTestPredictions(params),
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

export function useFutureForecast(params?: Record<string, unknown>, enabled = true) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["future-forecast", params ?? {}],
    queryFn: () => forecastApi.getFutureForecast(params),
    enabled,
    ...tier("windowed"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useModelMetrics(demandClass?: string) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["model-metrics", demandClass ?? ""],
    queryFn: () => forecastApi.getModelMetrics(demandClass),
    ...tier("static"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useTrainingSummary() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["training-summary"],
    queryFn: () => forecastApi.getTrainingSummary(),
    ...tier("static"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useClassSummary() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["class-summary"],
    queryFn: () => forecastApi.getClassSummary(),
    ...tier("static"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function usePipelineStatus() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["pipeline-status"],
    queryFn: () => forecastApi.getAllPipelineStatus(),
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

export function useTriggerPipeline() {
  const qc = useQueryClient();
  const train = useMutation({
    mutationFn: () => forecastApi.triggerTraining(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["pipeline-status"] }),
  });
  const inference = useMutation({
    mutationFn: () => forecastApi.triggerInference(),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["pipeline-status"] }),
  });
  return {
    triggerTrain: () => train.mutate(),
    triggerInference: () => inference.mutate(),
    loading: train.isPending || inference.isPending,
    error: train.error || inference.error ? String(train.error || inference.error) : null,
  };
}
