export interface DatasetInfo {
  exists: boolean;
  rows: number;
  first_date: string | null;
  last_date: string | null;
  file: string;
  size_mb: number;
}

export interface DataStatusResponse {
  success: boolean;
  datasets: Record<string, DatasetInfo>;
}

export interface ImportResponse {
  success: boolean;
  dataset: string;
  mode: string;
  new_rows: number;
  total_rows: number;
  file: string;
  duration_seconds: number;
  message: string;
  error: string;
}

export interface ImportAllResponse {
  success: boolean;
  results: Record<string, ImportResponse>;
}

// EDA -- sales overview
export interface SalesTotals {
  transactions: number;
  total_quantity: number;
  total_revenue: number;
  unique_routes: number;
  unique_items: number;
  unique_warehouses: number;
  unique_categories: number;
  first_date: string;
  last_date: string;
  // Distinct active dates inside the lookback slice (so the page can
  // render "N working days" alongside the date range).
  working_days: number;
}

export interface CatalogItem {
  ItemCode: string;
  ItemName: string;
  CategoryName: string;
  avg_price: number;
  last_price: number;
  total_quantity: number;
  transactions: number;
  last_seen: string;
}

export interface ItemCatalogResponse {
  available: boolean;
  count: number;
  items: CatalogItem[];
}

export interface ItemStatsWindow {
  avg: number | null;
  total: number;
  active_days: number;
  days: number;
}

// Four headline metrics for the executive dashboard.
//
// Tiles 1-3 are sales-only aggregates over sales_recent.csv. Tile 4
// (lost_opportunity) joins demand_forecast.csv with sales and uses the
// **reconciled van load** (V5_b: bias-corrected + clamped carry-over)
// as the "what we said to load" baseline, NOT the raw model output --
// the metric measures gap between recommended load and actual sold.
//
// Each metric ships both the aggregate AND the per-day average, with
// the denominator visible (working_days for 1-3, covered_days for 4)
// so the math is verifiable on the surface.
export interface BusinessKpis {
  available: boolean;
  message?: string;
  // Echo of the (start_date, end_date) the server filtered on.
  // ISO YYYY-MM-DD, same shape as the request.
  start_date?: string;
  end_date?: string;
  anchor_date?: string;
  // Distinct active sales dates inside the lookback slice -- denominator
  // for tiles 1/2/3 daily averages.
  working_days?: number;
  // (route, day) cells with a past forecast row -- denominator for tile 4.
  covered_routes?: number;
  covered_days?: number;

  total_revenue?: {
    available: boolean;
    amount?: number;
    daily_avg?: number | null;
  };

  total_volume?: {
    available: boolean;
    units?: number;
    transactions?: number;
    daily_avg_units?: number | null;
    daily_avg_transactions?: number | null;
  };

  unique_items?: {
    available: boolean;
    count?: number;
    // Mean of per-day nunique(items sold).
    daily_avg?: number | null;
    // Mean of per-day coverage ratios over the lookback window.
    avg_daily_coverage_pct?: number | null;
  };

  lost_opportunity?: {
    available: boolean;
    amount?: number;
    units?: number;
    items_affected?: number;
    daily_avg?: number | null;
  };
}

export interface ItemStatsResponse {
  available: boolean;
  message?: string;
  item_code?: string;
  route_code?: string | null;
  anchor_date?: string;
  total_transactions?: number;
  windows?: {
    last_week: ItemStatsWindow | null;
    last_4_weeks: ItemStatsWindow | null;
    last_3_months: ItemStatsWindow | null;
    last_6_months: ItemStatsWindow | null;
  };
}

export interface DailyTrendPoint {
  date: string;
  quantity: number;
  revenue: number;
}

export interface TopRoute {
  RouteCode: string;
  quantity: number;
  revenue: number;
  items: number;
}

export interface CategoryBreakdown {
  CategoryName: string;
  quantity: number;
  revenue: number;
}

export interface SalesOverviewResponse {
  available: boolean;
  message?: string;
  // Echo of the (start_date, end_date) the server filtered on.
  // ISO YYYY-MM-DD, same shape as the request.
  start_date?: string;
  end_date?: string;
  totals?: SalesTotals;
  daily_trend?: DailyTrendPoint[];
  // Backend returns these already sorted by revenue desc, capped at 10.
  // Frontend should NOT re-sort -- the response order is the contract.
  top_routes?: TopRoute[];
  categories?: CategoryBreakdown[];
}

// EDA -- cascading filter dimensions (warehouse → route → category → item)
export interface FilterDimensionOption {
  code: string;
  name: string;
  // Routes only: which warehouse the route belongs to. Lets the VanLoad
  // route grid group cards by warehouse without a second lookup.
  warehouse_code?: string;
  warehouse_name?: string;
}

export interface FilterDimensions {
  warehouses: FilterDimensionOption[];
  routes: FilterDimensionOption[];
  categories: FilterDimensionOption[];
  items: FilterDimensionOption[];
  /** Server-side trim of the input selection vector against the
   *  cascaded option sets. Frontend applies it verbatim -- no
   *  client-side validation pass against the available options. */
  trimmed_selections: DashboardFilters;
}

// Empty arrays mean "no filter applied" -- backend treats them as "all".
export interface DashboardFilters {
  warehouse_codes: string[];
  route_codes: string[];
  category_codes: string[];
  item_codes: string[];
}

export const EMPTY_FILTERS: DashboardFilters = {
  warehouse_codes: [],
  route_codes: [],
  category_codes: [],
  item_codes: [],
};

// Reporting period -- the user-chosen date window every dashboard tile
// and drawer slices its data against. Both bounds are ISO ``YYYY-MM-DD``
// in the user's local timezone (matches the rest of the wire). A single
// day is represented as ``start_date == end_date``; no separate type.
//
// Invariant enforced by the picker:
//   start_date <= end_date <= today
//
// Backend endpoints validate the same two regexes and reject otherwise.
export interface ReportingPeriod {
  start_date: string;
  end_date: string;
}

// Most recent date in sales_recent.csv. Drawers query this on open to
// seed defaults that always land on a date the data actually covers
// (so a Monday open never lands on a zero-data Saturday). Null when
// the CSV is empty -- callers fall back to today.
export interface LastActiveDateResponse {
  available: boolean;
  date: string | null;
}
