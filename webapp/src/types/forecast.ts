// ---- Reconciliation: past-performance chart ----

export interface PastPerformanceDaily {
  date: string;
  /** Rep's prior-day leftover: ClosingQty logged for (route, item, d-1), anchor-scoped. 0 when no row exists for d-1. Component of rep_van_load and recommended_van_load (same truck). */
  past_leftover: number;
  /** Today's depot-issued allocation (rep's new stock today, anchor-scoped). Component of rep_van_load only. */
  today_allocation: number;
  /** Rep's PHYSICAL truck reality = past_leftover + today_allocation. */
  rep_van_load: number;
  /** Same physical leftover as past_leftover (one truck, one reality). */
  recommended_carried: number;
  /** Engine's per-day fresh issuance under the rep's actual opening state. */
  recommended_fresh: number;
  /** Recommended composition = recommended_carried + recommended_fresh = past_leftover + recommended_fresh. The chart plots THIS line as "Recommended van load". */
  recommended_van_load: number;
  /** Invoiced demand for the day. */
  actual_sold: number;
}

export interface PastPerformanceTotals {
  /** Σ_d rep_van_load. Identity: rep_van_load_total = past_leftover_total + today_allocation_total. */
  rep_van_load_total: number;
  /** Σ_d past_leftover. Same physical leftover the rep had, summed over the window. */
  past_leftover_total: number;
  /** Σ_d today_allocation. Depot-issued component of rep_van_load. */
  today_allocation_total: number;
  /** Same physical leftover, surfaced under the recommendation label so the recommended composition reads carried + fresh. By construction recommended_carried_total === past_leftover_total. */
  recommended_carried_total: number;
  /** Σ_d recommended_fresh. Engine's per-day fresh issuance given the rep's real opening state. Typically smaller than today_allocation_total because the engine subtracts leftover from the bias-corrected forecast. */
  recommended_fresh_total: number;
  /** Identity: recommended_van_load_total = recommended_carried_total + recommended_fresh_total. The compositional number rendered on the headline tile, plotted on the chart, AND scored against actual_sold_total for forecast_accuracy_pct. */
  recommended_van_load_total: number;
  /** Σ_d actual_sold. Ground-truth demand for the window. */
  actual_sold_total: number;
  /** Σ_i max(rep_load_i − sold_i, 0) × price_i. AED tied up in rep's overnight stock. Per-item because AED requires per-item prices. */
  rep_holding_value: number;
  /** Σ_i max(our_load_i − sold_i, 0) × price_i. AED tied up under our recommendation. */
  our_holding_value: number;
  /** rep_holding_value − our_holding_value. Positive = our recommendation ties up fewer AED. */
  holding_savings: number;
  /** Aggregate-level overnight units. Identity: rep_excess_units = max(rep_van_load_total − actual_sold_total, 0). Reconciles with the headline tile by simple subtraction. */
  rep_excess_units: number;
  /** Identity: our_excess_units = max(recommended_van_load_total − actual_sold_total, 0). */
  our_excess_units: number;
  /** rep_excess_units − our_excess_units. Positive = our policy leaves fewer units on the truck overnight. */
  excess_units_savings: number;
  active_days: number;
}

export interface PastPerformanceMetrics {
  /** Bounded fill-ratio accuracy:
   *   min(recommended_van_load_total, actual_sold_total)
   *   / max(recommended_van_load_total, actual_sold_total) × 100
   * Symmetric (2× over and 0.5× under both score 50%), naturally in
   * [0, 100], same recommendation number as the headline tile. This is
   * an OPERATIONAL "did we load the right amount" metric -- intentionally
   * distinct from the model-quality WAPE used by drift detection. */
  forecast_accuracy_pct: number;
  /** Per-day mean of |sold ∩ predicted| / |sold| × 100. % of items the rep actually sold that we had a forecast for. */
  forecast_coverage_pct: number;
}

export interface PastPerformanceResponse {
  available: boolean;
  message?: string;
  route_code?: string;
  start_date?: string;
  end_date?: string;
  lookback_days?: number;
  active_days?: number;
  daily: PastPerformanceDaily[];
  totals: PastPerformanceTotals;
  metrics: PastPerformanceMetrics;
}

export interface PipelineRunResponse {
  success: boolean;
  message: string;
  config: string | null;
}

/* ---- Pipeline page resolver (single fetch, fully shaped) ---- */

export interface ResolvedPipelineStep {
  key: string;
  name: string;
  status: "idle" | "running" | "completed" | "failed" | "skipped";
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  last_success_duration_seconds: number | null;
  /** Server-rendered metric line (formatted text, ready to display). */
  metric_text: string | null;
  /** Server-rendered detail line. ``null`` for running steps -- the
   *  client renders ``Running for Xs`` from started_at + a per-second
   *  tick because that's animation, not calculation. */
  detail_text: string | null;
}

export interface ResolvedPipelineStatusResponse {
  success: boolean;
  any_running: boolean;
  steps: ResolvedPipelineStep[];
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
  // Apples-to-apples: raw model forecast vs invoiced actuals, scored
  // under the same composite function the training-time baseline uses.
  // Drift uses this for the recent vs baseline delta so the comparison
  // is honest -- not contaminated by reconciliation lift.
  recent_accuracy: number | null;
  baseline_accuracy: number | null;
  delta: number | null;
  source: "live" | "test_set" | "unavailable";
  // Operational lens on the same window: V5_b reconciled van-load vs
  // invoiced actuals. Always null on the test_set fallback path -- test
  // predictions don't have a van-load to reconcile against.
  recent_reconciled_accuracy: number | null;
  // Sample size that fed the recent score (cells where actual > 0 AND
  // predicted > 0). Surfaced so the UI can render "n cells scored".
  rows_compared: number | null;
}
