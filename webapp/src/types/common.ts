// Shared types across all modules.

export type Row = Record<string, unknown>;

// Summary responses (aggregated KPIs for dashboard)

export interface DataSummary {
  datasets: Record<
    string,
    { exists: boolean; rows: number; last_date: string | null; size_mb: number }
  >;
  total_rows: number;
  db_connected: boolean;
  last_updated: string | null;
}

export interface TrainingClassWinner {
  /** SBC class name (lowercase: ``smooth``/``intermittent``/``erratic``/``lumpy``). */
  demand_class: string;
  /** Best model's name for this class (e.g. ``"xgboost"``). */
  best_model: string;
  /** Best model's tolerance-adjusted WAPE for this class, rounded to 1dp.
   *  Accuracy below comes from ``composite_summary`` server-side; render
   *  it directly instead of recomputing ``100 - wape``. */
  wape: number;
  /** Server-computed accuracy_pct = ``max(0, 100 - wape)``. Pure render
   *  contract: the webapp must NOT recompute this client-side. */
  accuracy_pct: number;
  /** How many candidate models competed in this class's tournament. */
  models_competed: number;
}

export interface TrainingOverview {
  trained_at?: string | null;
  test_date_start?: string | null;
  test_date_end?: string | null;
  test_routes?: number | null;
  test_items?: number | null;
  /** Per-class winners with their WAPE. Server-sorted by class name --
   *  the subtitle re-sorts by descending WAPE/accuracy presentation. */
  class_winners?: TrainingClassWinner[] | null;
  total_models_trained?: number | null;
  feature_count?: number | null;
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
