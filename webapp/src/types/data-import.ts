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

export interface ImportRequest {
  dataset: string;
  mode: "incremental" | "full";
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

export interface DataHealthResponse {
  status: string;
  db_connected: boolean;
  data_dir: string;
  datasets_available: number;
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
  days_covered: number;
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

export interface BusinessKpis {
  available: boolean;
  message?: string;
  anchor_date?: string;
  yesterday?: {
    revenue: number;
    delta_pct_vs_last_week: number | null;
    comparison_label: string;
  };
  last_7_days?: {
    revenue: number;
    delta_pct_vs_prior_7d: number | null;
    prior_revenue: number;
  };
  forecast_accuracy_7d?: {
    available: boolean;
    message?: string;
    window_days?: number;
    rows_compared?: number;
    accuracy_pct?: number | null;
  };
  today_operations?: {
    available: boolean;
    message?: string;
    window_days?: number;
    start_date?: string;
    end_date?: string;
    routes?: number;
    customers?: number;
    days_active?: number;
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

export interface TopItem {
  ItemCode: string;
  ItemName: string;
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
  totals?: SalesTotals;
  daily_trend?: DailyTrendPoint[];
  top_items?: TopItem[];
  top_routes?: TopRoute[];
  categories?: CategoryBreakdown[];
}

// EDA -- customer overview
export interface CustomerTotals {
  lookback_days: number;
  active_customers: number;
  total_visits: number;
  total_quantity: number;
  avg_visits_per_customer: number;
}

export interface TopCustomer {
  customer_code: string;
  customer_name: string;
  route_code: string;
  visits: number;
  unique_items: number;
  total_quantity: number;
  last_purchase: string;
}

export interface CustomersByRoute {
  route_code: string;
  customers: number;
  total_quantity: number;
}

export interface CustomerOverviewResponse {
  available: boolean;
  message?: string;
  totals?: CustomerTotals;
  top_customers?: TopCustomer[];
  by_route?: CustomersByRoute[];
}
