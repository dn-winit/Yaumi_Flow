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
  /** sum_d rep_van_load. Identity: rep_van_load_total = past_leftover_total + today_allocation_total. */
  rep_van_load_total: number;
  /** sum_d past_leftover. Same physical leftover the rep had, summed over the window. */
  past_leftover_total: number;
  /** sum_d today_allocation. Depot-issued component of rep_van_load. */
  today_allocation_total: number;
  /** Same physical leftover, surfaced under the recommendation label so the recommended composition reads carried + fresh. By construction recommended_carried_total === past_leftover_total. */
  recommended_carried_total: number;
  /** sum_d recommended_fresh. Engine's per-day fresh issuance given the rep's real opening state. Typically smaller than today_allocation_total because the engine subtracts leftover from the bias-corrected forecast. */
  recommended_fresh_total: number;
  /** Identity: recommended_van_load_total = recommended_carried_total + recommended_fresh_total. The compositional number rendered on the headline tile, plotted on the chart, AND scored against actual_sold_total for forecast_accuracy_pct. */
  recommended_van_load_total: number;
  /** sum_d actual_sold. Ground-truth demand for the window. */
  actual_sold_total: number;
  /** sum_i max(rep_load_i - sold_i, 0) * price_i. AED tied up in rep's overnight stock. Per-item because AED requires per-item prices. */
  rep_holding_value: number;
  /** sum_i max(our_load_i - sold_i, 0) * price_i. AED tied up under our recommendation. */
  our_holding_value: number;
  /** rep_holding_value - our_holding_value. Positive = our recommendation ties up fewer AED. */
  holding_savings: number;
  /** Aggregate-level overnight units. Identity: rep_excess_units = max(rep_van_load_total - actual_sold_total, 0). Reconciles with the headline tile by simple subtraction. */
  rep_excess_units: number;
  /** Identity: our_excess_units = max(recommended_van_load_total - actual_sold_total, 0). */
  our_excess_units: number;
  /** rep_excess_units - our_excess_units. Positive = our policy leaves fewer units on the truck overnight. */
  excess_units_savings: number;
  /** Canonical end-of-window leftover sum -- the units that carry into
   *  the day AFTER the window. Wire-authoritative; the client never
   *  recomputes this from items[*].leftover_to_next_day. */
  leftover_to_next_day_total: number;
  active_days: number;
}

export interface PastPerformanceMetrics {
  /** Bounded fill-ratio accuracy:
   *   min(recommended_van_load_total, actual_sold_total)
   *   / max(recommended_van_load_total, actual_sold_total) * 100
   * Symmetric (2x over and 0.5x under both score 50%), naturally in
   * [0, 100], same recommendation number as the headline tile. This is
   * an OPERATIONAL "did we load the right amount" metric -- intentionally
   * distinct from the model-quality WAPE used by drift detection. */
  forecast_accuracy_pct: number;
  /** Per-day mean of |sold AND predicted| / |sold| * 100. % of items the rep actually sold that we had a forecast for. */
  forecast_coverage_pct: number;
}

/**
 * Per-(item, date) row backing the click-to-explain popovers on every
 * aggregate tile in the Past Performance drawer and the VanLoad summary.
 * Pre-sorted and pre-aggregated server-side. The wire is the single
 * source of truth -- the client does not re-sort, sum, or mutate.
 *
 * Each row carries the eight per-day measurements the BreakdownPopover
 * renders. Engine intermediates (forecast_corrected, expected_demand,
 * pattern_floor_applied, ...) live on ``table_rows[*].explain`` instead
 * -- that is the surface the ExplainabilityModal consumes, so the wire
 * carries each diagnostic exactly once.
 */
export interface PastPerformanceItem {
  itemCode: string;
  itemName: string;
  /** ISO YYYY-MM-DD date this row applies to. */
  date: string;
  /** Rep's physical van load on this date (carried + fresh). */
  rep_van_load: number;
  /** Rep's prior-day leftover that carried into this date. */
  past_leftover: number;
  /** Today's depot-issued allocation (rep's new stock). */
  today_allocation: number;
  /** Engine's recommended composition on this date. */
  recommended_van_load: number;
  /** Engine's carried-from-yesterday component (same physical leftover
   *  as past_leftover, surfaced under the recommendation label). */
  recommended_carried: number;
  /** Engine's fresh-from-depot component for this date. */
  recommended_fresh: number;
  /** Invoiced demand (actually sold) on this date. */
  actual_sold: number;
  /** Leftover carrying into the next day from this row. Always >= 0. */
  leftover_to_next_day: number;
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
  /** Non-optional on the wire; backend defaults to [] when no anchor
   *  items exist for the window. Rendered as the per-item breakdown
   *  table; hidden entirely when empty. */
  items: PastPerformanceItem[];
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
