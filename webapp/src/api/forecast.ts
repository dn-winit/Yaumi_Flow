import { getClient } from "./client";
import type {
  PipelineRunResponse,
  ResolvedPipelineStatusResponse,
  RetrainConfig,
  RetrainHistoryEntry,
  DriftStatus,
  PastPerformanceResponse,
  PastPerformanceItem,
} from "@/types/forecast";
import type { ForecastSummary } from "@/types/common";

export interface ForecastRouteSummary {
  route_code: string;
  skus: number;
  /**
   * Total van load for the route = ``opening_stock + recommended_load``
   * summed across the route's items. Same number ``VanLoadSummary``
   * shows when the route is selected, so the tile and the summary
   * always agree. Backend contract is reconciled-only -- no raw-
   * forecast field on the wire.
   */
  predicted_qty: number;
  /**
   * Day with the highest van-load total in the response window. Equals
   * the requested date when scoped to one day; surfaces the busiest
   * forward-day when the request is for the full horizon.
   */
  peak_day?: string | null;
}

/**
 * Top-level response shape for ``/predictions/forecast/route-summary``.
 * The ``reconciled`` flag is true when V5_b produced reconciled values
 * for every row in the response; false when the backend degraded to
 * raw forecast (bias table missing / cold start). The UI shows a
 * warning chip on the tiles when this is false.
 */
export interface ForecastRouteSummaryResponse {
  success: boolean;
  date?: string | null;
  routes: ForecastRouteSummary[];
  reconciled: boolean;
}

/* ---- VanLoad page view (one fetch per page state) ---- */

/**
 * KPI tile values for the VanLoad summary row. Server-computed and
 * server-checked: van_load_qty == carried_qty + issued_qty.
 */
export interface VanLoadSummaryView {
  van_load_qty: number;
  van_load_items: number;
  carried_qty: number;
  carried_items: number;
  issued_qty: number;
  issued_items: number;
  revenue: number | null;
  has_revenue: boolean;
  at_risk: number;
}

/** One bar in the 'Top N items by van load' chart, pre-sorted desc. */
export interface VanLoadChartItem {
  item_code: string;
  item_name: string;
  predicted: number;
}

/**
 * One row in the 'Van load items' table. Pre-sorted desc by load.
 * Single canonical field per concept -- the server has substituted
 * recommended_load -> units_to_load and the bound names so the client
 * never falls back across columns. ``has_real_confidence`` is the
 * server's verdict on whether the badge should render (real probability
 * vs synthetic 0/1).
 */
export interface VanLoadTableRow {
  item_code: string;
  item_name: string;
  units_to_load: number;
  p_demand: number | null;
  demand_class: string | null;
  lower_bound: number | null;
  upper_bound: number | null;
  has_real_confidence: boolean;
  /** Engine intermediates for the explainability modal:
   *  opening_stock, predicted_raw, forecast_corrected, bias_pct,
   *  recent_avg_per_selling_day, expected_demand,
   *  pattern_floor_applied, pattern_ceiling_applied,
   *  forecast_below_recent, guard_skipped. The table itself never
   *  reads these -- they're spread onto the legacy Row passed to
   *  ExplainabilityModal. */
  explain: Record<string, number | boolean | null>;
}

export interface VanLoadPageView {
  success: boolean;
  available: boolean;
  message?: string | null;
  route_code: string;
  date: string;
  reconciled: boolean;
  summary: VanLoadSummaryView;
  chart_top_n: VanLoadChartItem[];
  table_rows: VanLoadTableRow[];
  /** Per-(item, date) rows feeding the click-to-explain popovers on the
   *  VanLoadSummary tile. Single-date here, so every row's ``date`` is
   *  the page's ``date`` -- the field is kept for shape parity with the
   *  past-performance payload. Non-optional on the wire; backend
   *  defaults to []. */
  items: PastPerformanceItem[];
}

/* ---- ForecastDrawer (Upcoming plan) page view ---- */

export interface ForecastDrawerSummary {
  horizon_days: number;
  total_van_load: number;
  skus: number;
  avg_per_day: number;
  window_start: string | null;
  window_end: string | null;
  line_count: number;
}

export interface ForecastDrawerChartPoint {
  date: string;
  predicted: number;
  q10: number;
  q90: number;
}

export interface ForecastDrawerTableRow {
  date: string;
  item_code: string;
  item_name: string;
  units_to_load: number;
  p_demand: number | null;
  demand_class: string | null;
  lower_bound: number | null;
  upper_bound: number | null;
  has_real_confidence: boolean;
  explain: Record<string, number | null>;
}

export interface ForecastDrawerView {
  success: boolean;
  available: boolean;
  message?: string | null;
  route_code: string | null;
  item_codes: string[];
  from_date: string;
  show_band: boolean;
  reconciled: boolean;
  summary: ForecastDrawerSummary;
  chart_data: ForecastDrawerChartPoint[];
  table_rows: ForecastDrawerTableRow[];
}

const c = () => getClient("forecast");

/**
 * Frontend surface for the demand-forecasting-pipeline service. Trimmed
 * to what the Pipeline page + VanLoad drawers actually consume. Model
 * inspection / pair-explainability / cache-invalidation endpoints exist
 * server-side but had no UI consumer; if a consumer comes back, re-add
 * the method alongside the new caller.
 */
export const forecastApi = {
  getSummary: () => c().get<ForecastSummary>("/summary").then((r) => r.data),

  getForecastRouteSummary: (date?: string) =>
    c()
      .get<ForecastRouteSummaryResponse>(
        "/predictions/forecast/route-summary",
        { params: date ? { date } : {} },
      )
      .then((r) => r.data),

  /**
   * VanLoad route-detail page view -- one canonical fetch carrying the
   * summary tiles, the top-N chart, and the table rows. All numbers are
   * pre-computed and pre-sorted server-side; the page binds and renders.
   */
  getVanLoadPageView: (routeCode: string, date: string, topN: number = 10) =>
    c()
      .get<VanLoadPageView>("/page-views/van-load", {
        params: { route_code: routeCode, date, top_n: topN },
      })
      .then((r) => r.data),

  /**
   * Upcoming-plan drawer page view -- carries tiles, daily chart series,
   * line-item table and the show_band flag in one fetch.
   */
  getForecastDrawerPageView: (
    routeCode: string | undefined,
    itemCodes: string[] | undefined,
    fromDate: string | undefined,
  ) =>
    c()
      .get<ForecastDrawerView>("/page-views/forecast-drawer", {
        params: {
          ...(routeCode ? { route_code: routeCode } : {}),
          ...(itemCodes && itemCodes.length ? { item_codes: itemCodes } : {}),
          ...(fromDate ? { from_date: fromDate } : {}),
        },
      })
      .then((r) => r.data),

  triggerTraining: () =>
    c().post<PipelineRunResponse>("/pipeline/train", {}).then((r) => r.data),

  triggerInference: () =>
    c().post<PipelineRunResponse>("/pipeline/inference", {}).then((r) => r.data),

  /**
   * Pipeline page resolver -- one fetch carries: the per-step status
   * with formatted metric / detail strings, the publish-cascade
   * worst-of-two summary, and the global ``any_running`` flag. The
   * page renders the response verbatim; no client-side resolution.
   */
  getResolvedPipelineStatus: () =>
    c()
      .get<ResolvedPipelineStatusResponse>("/pipeline/resolved-status")
      .then((r) => r.data),

  getRetrainConfig: () =>
    c().get<RetrainConfig & { drift: DriftStatus }>("/retrain/config").then((r) => r.data),

  updateRetrainConfig: (updates: Partial<RetrainConfig>) =>
    c().post<RetrainConfig>("/retrain/config", updates).then((r) => r.data),

  getRetrainHistory: () =>
    c()
      .get<{ history: RetrainHistoryEntry[] }>("/retrain/history")
      .then((r) => r.data?.history ?? []),

  /**
   * Past-performance for the AccuracyDrawer -- single canonical source.
   * Returns three daily series (allocated, reconciled, sold), aggregate
   * tiles, and per-item top-N over the same window. Optional ``filters``
   * scope every series and item to a category / item-code subset
   * server-side, so chart and tile totals always reconcile.
   *
   * Wire shape mirrors ``/eda/sales`` and ``/analytics/adoption``: every
   * date-bounded endpoint takes ``(start_date, end_date)`` so the
   * frontend never has to translate between calendar deltas and ISO
   * dates at the network boundary.
   */
  getReconciliationPastPerformance: (
    routeCode: string,
    startDate: string,
    endDate: string,
    filters?: { item_codes?: string[]; category_codes?: string[] },
  ) =>
    c()
      .get<PastPerformanceResponse>("/reconciliation/past-performance", {
        params: {
          route_code: routeCode,
          start_date: startDate,
          end_date: endDate,
          ...(filters?.item_codes?.length ? { item_codes: filters.item_codes } : {}),
          ...(filters?.category_codes?.length ? { category_codes: filters.category_codes } : {}),
        },
      })
      .then((r) => r.data),
};
