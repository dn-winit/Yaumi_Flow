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

export interface ClassSummaryResponse {
  success: boolean;
  total_pairs: number;
  classes: Record<string, number>;
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
