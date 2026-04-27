// Shared types across all modules.

export type Row = Record<string, unknown>;

// Summary responses (aggregated KPIs for dashboard)

export interface DataSummary {
  datasets: Record<string, { exists: boolean; rows: number; last_date: string | null; size_mb: number }>;
  total_rows: number;
  db_connected: boolean;
  last_updated: string | null;
}

export interface ForecastSummary {
  accuracy_pct: number;
  total_pairs: number;
  classes: Record<string, number>;
  test_predictions_count: number;
  future_forecast_count: number;
  last_forecast_date: string | null;
  training_summary_exists: boolean;
}

export interface RecommendationSummary {
  routes_configured: number;
  last_generated_date: string | null;
  total_recs_latest_date: number;
  routes_with_recs_latest: number;
  customers_latest: number;
}
