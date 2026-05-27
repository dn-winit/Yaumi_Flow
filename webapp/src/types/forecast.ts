// ---- Reconciliation: past-performance chart ----

export interface PastPerformanceDaily {
  date: string;
  /** Rep's PHYSICAL truck total for the day (carry + fresh). */
  rep_van_load: number;
  /** Engine's recommended truck total (carry + fresh); plotted as "Recommended van load". */
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
  /** sum_i min(actual_sold_i, recommended_van_load_i); powers "% of customers served". */
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

/** Per-(item, date) row for click-to-explain popovers; server-sorted, never client-mutated.
 *  Explain-modal diagnostics live on `table_rows[*].explain`, not here. */
export interface PastPerformanceItem {
  itemCode: string;
  itemName: string;
  /** Category; empty -> rendered as "Uncategorised" in the rollup. */
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
  /** Rep's prior-day leftover (VW_GET_CLOSING_STOCK); null on pre-backfill / future dates. */
  yaumi_opening_stock?: number | null;
  /** Rep's fresh depot allocation (VW_GET_LOAD_ALLOCATION_DETAILS); same null semantics. */
  yaumi_fresh_load?: number | null;
  /** Rep's actual total van load (yaumi_total_van_load = opening + fresh). */
  yaumi_total_van_load?: number | null;
  /** Rep's end-of-day leftover (yaumi_leftover) after sales / returns. */
  yaumi_leftover?: number | null;
  /** True when the (route, item) was zeroed by the dormancy guard; null on legacy rows. */
  forecast_dormant?: boolean | null;
}

/** Per-category rollup; sum(categories[*]) == sum(items[*]) by construction. Sorted desc. */
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
  /** Per-category rollup; empty when items is empty. */
  categories: PastPerformanceCategoryRow[];
  /** Non-optional on the wire; defaults to []. Per-item breakdown table hides when empty. */
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
  /** Server-rendered detail; null while running (client animates "Running for Xs"). */
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
  /** Origin: "manual" | "schedule" | "drift" | "scheduled" (legacy). */
  trigger: string;
  accuracy_before: number | null;
  accuracy_after: number | null;
  duration_seconds: number;
  /** "success" | "failed"; for success see promotion_decision for live status. */
  status: string;
  /** Snapshot id under versions/<version_id>/; null on legacy rows. */
  version_id?: string | null;
  /** Gate verdict: "promote" | "reject" | "cold_start" | null (gate disabled). */
  promotion_decision?: "promote" | "reject" | "cold_start" | string | null;
  /** Human-readable rationale shown in the decision-badge tooltip. */
  promotion_reason?: string | null;
}

export interface DriftStatus {
  status: "stable" | "drifting" | "significant";
  // Raw model forecast vs invoiced actuals (no reconciliation lift) -- honest drift signal.
  recent_accuracy: number | null;
  baseline_accuracy: number | null;
  delta: number | null;
  source: "live" | "test_set" | "unavailable";
  // V5_b reconciled van-load vs actuals; null on test_set fallback.
  recent_reconciled_accuracy: number | null;
  // Cells where actual>0 AND predicted>0; powers the "n cells scored" UI string.
  rows_compared: number | null;
}
