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
  /** Reconciled total van load for the route (opening_stock + recommended_load). */
  predicted_qty: number;
  /** Peak day inside the response window (== request date when scoped to one day). */
  peak_day?: string | null;
}

/** Response for /predictions/forecast/route-summary. `reconciled=false` -> UI warning chip. */
export interface ForecastRouteSummaryResponse {
  success: boolean;
  date?: string | null;
  routes: ForecastRouteSummary[];
  reconciled: boolean;
}

/* ---- VanLoad page view (one fetch per page state) ---- */

/** KPI tile values; server-checked: van_load_qty == carried_qty + issued_qty. */
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
 * One row in 'Van load items', pre-sorted desc by total truck weight.
 * `units_to_load` = fresh allocation; `recommended_van_load` = total truck weight
 * (= ceil(opening_stock) + units_to_load). `has_real_confidence` controls badge rendering.
 */
export interface VanLoadTableRow {
  item_code: string;
  item_name: string;
  units_to_load: number;
  recommended_van_load: number;
  p_demand: number | null;
  demand_class: string | null;
  lower_bound: number | null;
  upper_bound: number | null;
  has_real_confidence: boolean;
  /** Spread onto the legacy Row for ExplainabilityModal: opening_stock,
   *  recent_avg_per_selling_day, expected_demand, forecast_below_recent, guard_skipped. */
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
  /** Per-(item, date) rows for VanLoadSummary click-to-explain popovers;
   *  shape parity with past-performance. Backend defaults to []. */
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

/** Frontend surface for demand-forecasting-pipeline; trimmed to what the UI consumes. */
export const forecastApi = {
  getSummary: () => c().get<ForecastSummary>("/summary").then((r) => r.data),

  getForecastRouteSummary: (date?: string) =>
    c()
      .get<ForecastRouteSummaryResponse>(
        "/predictions/forecast/route-summary",
        { params: date ? { date } : {} },
      )
      .then((r) => r.data),

  /** VanLoad page view -- summary tiles + top-N chart + table rows in one fetch. */
  getVanLoadPageView: (routeCode: string, date: string, topN: number = 10) =>
    c()
      .get<VanLoadPageView>("/page-views/van-load", {
        params: { route_code: routeCode, date, top_n: topN },
      })
      .then((r) => r.data),

  /** Upcoming-plan drawer page view -- tiles + daily chart + line items in one fetch. */
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

  /** Pipeline page resolver -- per-step status, cascade summary, any_running in one fetch. */
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

  /** Past-performance for AccuracyDrawer -- three daily series + tiles + top-N items,
   *  optionally filtered by category/item-codes so chart and tile totals reconcile. */
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
