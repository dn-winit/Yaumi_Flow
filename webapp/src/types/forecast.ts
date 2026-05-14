// ---- Reconciliation: past-performance chart ----

export interface PastPerformanceDaily {
  date: string;
  /** Rep's PHYSICAL truck total for the day (carry + fresh). */
  rep_van_load: number;
  /** Engine's recommended truck total for the day (carry + fresh). Plotted as the "Recommended van load" bar. */
  recommended_van_load: number;
  /** Invoiced demand for the day. */
  actual_sold: number;
  /** Sum across items: max(rep_van_load - actual_sold, 0). Day-level rep leftover. */
  actual_leftover: number;
  /** Sum across items: max(recommended_van_load - actual_sold, 0). Day-level our leftover. */
  recommended_leftover: number;
}

export interface PastPerformanceTotals {
  /** sum_d rep_van_load. What the rep physically loaded across the window. */
  rep_van_load_total: number;
  /** sum_d recommended_van_load. What the engine would have loaded across the window. */
  recommended_van_load_total: number;
  /** sum_d actual_sold. Ground-truth demand for the window. */
  actual_sold_total: number;
  /** sum_i min(actual_sold_i, recommended_van_load_i). Units of demand the recommendation would have actually filled (capped by what was on the truck). Used server-side to compute the "% of customers served" share in the insight banner. */
  served_units: number;
  /** Working days inside the window (denominator for any per-day mean). */
  active_days: number;
  /** Per-(item, date) max(rep_van_load - actual_sold, 0) summed. Units the rep left over. */
  rep_leftover_units: number;
  /** Per-(item, date) max(recommended_van_load - actual_sold, 0) summed. Units we'd have left over. */
  our_leftover_units: number;
  /** rep_leftover_units - our_leftover_units. Positive = our policy leaves fewer units. */
  leftover_units_saved: number;
  /** leftover_units_saved / rep_leftover_units * 100, rounded. 0 when rep_leftover_units is 0. */
  leftover_pct_saved: number;
  /** Count of distinct SKUs the rep actually sold across the window (sum(actual_sold) > 0). */
  skus_sold: number;
  /** Of skus_sold, count of SKUs our recommendation also covered (sum(recommended_van_load) > 0). */
  skus_covered: number;
  /** skus_covered / skus_sold * 100, rounded. 0 when skus_sold is 0. */
  skus_coverage_pct: number;
}

/**
 * Per-(item, date) row backing the click-to-explain popovers on every
 * aggregate tile in the Past Performance drawer and the VanLoad summary.
 * Pre-sorted and pre-aggregated server-side. The wire is the single
 * source of truth -- the client does not re-sort, sum, or mutate.
 *
 * Each row carries the eight per-day measurements the drawer table
 * renders. Explain-modal fields (expected_demand, recent_avg_per_selling_day,
 * forecast_below_recent, ...) live on ``table_rows[*].explain`` instead
 * -- one diagnostic, one home on the wire.
 */
export interface PastPerformanceItem {
  itemCode: string;
  itemName: string;
  /** Category the item belongs to. Empty string when the catalogue
   *  has no CategoryName for this code -- rendered as "Uncategorised"
   *  in the category rollup. */
  categoryName: string;
  /** ISO YYYY-MM-DD date this row applies to. */
  date: string;
  /** Rep's physical van load on this date (carry + fresh). */
  rep_van_load: number;
  /** Engine's recommended van load on this date (carry + fresh). */
  recommended_van_load: number;
  /** Invoiced demand (actually sold) on this date. */
  actual_sold: number;
  /** Naive leftover under the rep's load: max(rep_van_load - actual_sold, 0). */
  actual_leftover: number;
  /** Naive leftover under the engine's recommendation: max(recommended_van_load - actual_sold, 0). */
  recommended_leftover: number;
  /** Rep's prior-day leftover (yf_sales_transactions.yaumi_opening_stock),
   *  sourced from VW_GET_CLOSING_STOCK. ``null`` for dates predating the
   *  yaumi_* backfill or future dates with no rep activity. */
  yaumi_opening_stock?: number | null;
  /** Rep's fresh depot allocation (yaumi_fresh_load), sourced from
   *  VW_GET_LOAD_ALLOCATION_DETAILS. Same null semantics as above. */
  yaumi_fresh_load?: number | null;
  /** Rep's actual total van load (yaumi_total_van_load = opening + fresh). */
  yaumi_total_van_load?: number | null;
  /** Rep's end-of-day leftover (yaumi_leftover) after sales / returns. */
  yaumi_leftover?: number | null;
  /** Dormancy guard flag from yf_sales_transactions.forecast_dormant.
   *  ``true`` when the (route, item) pair had zero sales across its
   *  route's last N trip days and the engine zeroed expected_demand
   *  for it. ``null`` on legacy rows predating the dormancy backfill. */
  forecast_dormant?: boolean | null;
}

/** Per-category rollup row, aggregated server-side from items_payload.
 *  Identity by construction: sum(categories[*].field) == sum(items[*].field)
 *  for every numeric field below. Sorted by recommended_van_load desc. */
export interface PastPerformanceCategoryRow {
  categoryName: string;
  /** Count of distinct itemCodes in the category with any activity. */
  skus: number;
  rep_van_load: number;
  recommended_van_load: number;
  actual_sold: number;
  actual_leftover: number;
  recommended_leftover: number;
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
  /** Per-category rollup across the whole window. Empty when items
   *  is empty. Rendered as a collapsible category breakdown table. */
  categories: PastPerformanceCategoryRow[];
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
