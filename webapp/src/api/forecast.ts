import { getClient } from "./client";
import type {
  PredictionResponse,
  MetricsResponse,
  TrainingSummaryResponse,
  ModelFilesResponse,
  ClassSummaryResponse,
  ExplainabilityResponse,
  PipelineRunResponse,
  PipelineStatusResponse,
  ForecastHealthResponse,
  AccuracyComparisonResponse,
} from "@/types/forecast";
import type { ForecastSummary } from "@/types/common";

export interface ForecastRouteSummary {
  route_code: string;
  skus: number;
  predicted_qty: number;
}

const c = () => getClient("forecast");

export interface AccuracyParams {
  start_date?: string;
  end_date?: string;
  route_code?: string;
  item_code?: string;
  limit?: number;
}

export const forecastApi = {
  // Summary (aggregated KPIs)
  getSummary: () => c().get<ForecastSummary>("/summary").then((r) => r.data),

  // Live accuracy: predicted (yf_demand_forecast) vs actual (YaumiLive)
  getAccuracyComparison: (params?: AccuracyParams) =>
    c().get<AccuracyComparisonResponse>("/accuracy/comparison", { params }).then((r) => r.data),

  // Predictions
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

  // Metrics
  getModelMetrics: (demand_class?: string) =>
    c().get<MetricsResponse>("/metrics/models", { params: { demand_class } }).then((r) => r.data),

  // Models
  getTrainingSummary: () =>
    c().get<TrainingSummaryResponse>("/models/summary").then((r) => r.data),

  getModelFiles: () =>
    c().get<ModelFilesResponse>("/models/files").then((r) => r.data),

  getPairModelLookup: (params?: Record<string, unknown>) =>
    c().get<ExplainabilityResponse>("/models/pair-lookup", { params }).then((r) => r.data),

  // Explainability
  getClassSummary: () =>
    c().get<ClassSummaryResponse>("/explainability/classes/summary").then((r) => r.data),

  getPairClasses: (demand_class?: string) =>
    c().get<ExplainabilityResponse>("/explainability/classes", { params: { demand_class } }).then((r) => r.data),

  getPairExplainability: (params?: Record<string, unknown>) =>
    c().get<ExplainabilityResponse>("/explainability/pairs", { params }).then((r) => r.data),

  // Pipeline
  triggerTraining: () =>
    c().post<PipelineRunResponse>("/pipeline/train", {}).then((r) => r.data),

  triggerInference: () =>
    c().post<PipelineRunResponse>("/pipeline/inference", {}).then((r) => r.data),

  getPipelineStatus: (name: string) =>
    c().get<PipelineStatusResponse>(`/pipeline/status/${name}`).then((r) => r.data),

  getAllPipelineStatus: () =>
    c().get<Record<string, PipelineStatusResponse>>("/pipeline/status").then((r) => r.data),

  invalidateCache: () =>
    c().post("/pipeline/invalidate-cache").then((r) => r.data),

  getHealth: () =>
    c().get<ForecastHealthResponse>("/health").then((r) => r.data),
};
