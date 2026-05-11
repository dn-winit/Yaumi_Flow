// Shared types across all modules.

export type Row = Record<string, unknown>;

// Summary responses (aggregated KPIs for dashboard)

export interface DataSummary {
  datasets: Record<string, { exists: boolean; rows: number; last_date: string | null; size_mb: number }>;
  total_rows: number;
  db_connected: boolean;
  last_updated: string | null;
}

export interface TrainingOverview {
  trained_at?: string | null;
  test_date_start?: string | null;
  test_date_end?: string | null;
  test_routes?: number | null;
  test_items?: number | null;
}

export interface ForecastSummary {
  // Nullable: 0% / 0 pairs is rendered as "—" by callers, so the API
  // returns null until artifacts that back these numbers exist (avoids
  // a misleading mid-training "0%" baseline).
  accuracy_pct: number | null;
  total_pairs: number | null;
  classes: Record<string, number>;
  test_predictions_count: number;
  future_forecast_count: number;
  last_forecast_date: string | null;
  training_summary_exists: boolean;
  training_overview?: TrainingOverview | null;
}

export interface RecommendationSummary {
  routes_configured: number;
  last_generated_date: string | null;
  total_recs_latest_date: number;
  routes_with_recs_latest: number;
  customers_latest: number;
}
