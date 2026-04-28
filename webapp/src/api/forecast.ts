import { getClient } from "./client";
import type {
  PredictionResponse,
  MetricsResponse,
  ClassSummaryResponse,
  PipelineRunResponse,
  PipelineStatusResponse,
  RetrainConfig,
  RetrainHistoryEntry,
  DriftStatus,
} from "@/types/forecast";
import type { ForecastSummary } from "@/types/common";

export interface ForecastRouteSummary {
  route_code: string;
  skus: number;
  predicted_qty: number;
}

const c = () => getClient("forecast");

/**
 * Frontend surface for the demand-forecasting-pipeline service. Trimmed
 * to what the Pipeline page + VanLoad drawers actually consume. Model
 * inspection / pair-explainability / cache-invalidation endpoints exist
 * server-side but had no UI consumer; if a consumer comes back, re-add
 * the method alongside the new caller.
 */
export const forecastApi = {
  getSummary: () => c().get<ForecastSummary>("/summary").then((r) => r.data),

  getTestPredictions: (params?: Record<string, unknown>) =>
    c().get<PredictionResponse>("/predictions/test", { params }).then((r) => r.data),

  getFutureForecast: (params?: Record<string, unknown>) =>
    c().get<PredictionResponse>("/predictions/forecast", { params }).then((r) => r.data),

  getForecastRouteSummary: (date?: string) =>
    c()
      .get<{ success: boolean; date: string | null; routes: ForecastRouteSummary[] }>(
        "/predictions/forecast/route-summary",
        { params: date ? { date } : {} },
      )
      .then((r) => r.data),

  getModelMetrics: (demand_class?: string) =>
    c().get<MetricsResponse>("/metrics/models", { params: { demand_class } }).then((r) => r.data),

  getClassSummary: () =>
    c().get<ClassSummaryResponse>("/explainability/classes/summary").then((r) => r.data),

  triggerTraining: () =>
    c().post<PipelineRunResponse>("/pipeline/train", {}).then((r) => r.data),

  triggerInference: () =>
    c().post<PipelineRunResponse>("/pipeline/inference", {}).then((r) => r.data),

  getAllPipelineStatus: () =>
    c().get<Record<string, PipelineStatusResponse>>("/pipeline/status").then((r) => r.data),

  getRetrainConfig: () =>
    c().get<RetrainConfig & { drift: DriftStatus }>("/retrain/config").then((r) => r.data),

  updateRetrainConfig: (updates: Partial<RetrainConfig>) =>
    c().post<RetrainConfig>("/retrain/config", updates).then((r) => r.data),

  getRetrainHistory: () =>
    c()
      .get<{ history: RetrainHistoryEntry[] }>("/retrain/history")
      .then((r) => r.data?.history ?? []),
};
