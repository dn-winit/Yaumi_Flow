export interface PredictionResponse {
  success: boolean;
  source: string;
  total: number;
  data: Record<string, unknown>[];
}

export interface MetricsResponse {
  success: boolean;
  total: number;
  data: Record<string, unknown>[];
}

export interface TrainingSummaryResponse {
  success: boolean;
  data: Record<string, unknown>;
}

export interface ModelFile {
  filename: string;
  size_bytes: number;
  modified: number;
  type: string;
}

export interface ModelFilesResponse {
  success: boolean;
  total: number;
  files: ModelFile[];
}

export interface ClassSummaryResponse {
  success: boolean;
  total_pairs: number;
  classes: Record<string, number>;
}

export interface ExplainabilityResponse {
  success: boolean;
  total: number;
  data: Record<string, unknown>[];
}

export interface PipelineRunResponse {
  success: boolean;
  message: string;
  config: string | null;
}

export interface PipelineStatusResponse {
  pipeline: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number;
  error: string | null;
}

export interface ForecastHealthResponse {
  status: string;
  artifacts: Record<string, boolean>;
  pipelines: Record<string, string>;
  config_path: string;
}

export interface AccuracyRow {
  trx_date: string;
  route_code: string;
  item_code: string;
  item_name: string;
  demand_class: string;
  model_used: string;
  predicted: number;
  lower_bound: number;
  upper_bound: number;
  actual_qty: number;
  variance: number;
  variance_pct: number;
}

export interface AccuracySummary {
  rows_compared: number;
  total_predicted: number;
  total_actual: number;
  mae: number;
  rmse: number;
  wape: number;
  accuracy_pct: number;
}

export interface AccuracyComparisonResponse {
  success: boolean;
  rows: AccuracyRow[];
  summary: AccuracySummary;
  error?: string;
}

/* ---- Auto-retrain ---- */

export interface RetrainConfig {
  enabled: boolean;
  frequency_days: number;
  last_auto_retrain: string | null;
  next_scheduled: string | null;
  auto_inference_after_train: boolean;
}

export interface RetrainHistoryEntry {
  date: string;
  trigger: string;
  accuracy_before: number | null;
  accuracy_after: number | null;
  duration_seconds: number;
  status: string;
}

export interface DriftStatus {
  status: "stable" | "drifting" | "significant";
  recent_accuracy: number | null;
  baseline_accuracy: number | null;
  delta: number | null;
  source: "live" | "test_set" | "unavailable";
}
