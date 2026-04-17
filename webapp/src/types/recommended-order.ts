export interface RecommendationItem {
  TrxDate: string;
  RouteCode: string;
  CustomerCode: string;
  CustomerName: string;
  ItemCode: string;
  ItemName: string;
  RecommendedQuantity: number;
  Tier: string;
  VanLoad: number;
  PriorityScore: number;
  AvgQuantityPerVisit: number;
  DaysSinceLastPurchase: number;
  PurchaseCycleDays: number;
  FrequencyPercent: number;
  PatternQuality: number;
  PurchaseCount: number;
  TrendFactor?: number;
  ReasonStatus?: string;
  ReasonExplanation?: string;
}

export interface GenerateRequest {
  date: string;
  route_codes?: string[];
  force?: boolean;
}

export interface GenerateResponse {
  success: boolean;
  message: string;
  date: string;
  routes_processed: number;
  total_records: number;
  duration_seconds: number;
  details: Record<string, unknown>[];
}

export interface RetrieveRequest {
  date: string;
  route_code?: string;
  customer_code?: string;
  item_code?: string;
  tier?: string;
  min_priority?: number;
  limit?: number;
  offset?: number;
}

export interface RetrieveResponse {
  success: boolean;
  date: string;
  total: number;
  data: RecommendationItem[];
  source: "store" | "generated";
  generated_routes: number;
}

export interface ExistsResponse {
  date: string;
  exists: Record<string, boolean>;
}

export interface GenerationInfoResponse {
  exists: boolean;
  date: string;
  total_records: number;
  routes_count: number;
  customers_count: number;
  items_count: number;
  generated_at?: string;
}

export interface FilterOptionsResponse {
  routes: string[];
  journey_counts: Record<string, number>;
}

export interface OrderHealthResponse {
  status: string;
  last_refresh: string | null;
  route_codes: string[];
}

// ---------- Analytics: Adoption ----------
export interface AdoptionSummary {
  rows_recommended: number;
  rows_adopted: number;
  rows_over_recommended: number;
  rows_missed: number;
  adoption_pct: number | null;
  uplift_pct: number | null;
  total_recommended_qty: number;
  total_sold_qty: number;
}
export interface AdoptionDailyPoint {
  date: string;
  recommended: number;
  adopted: number;
  adoption_pct: number;
}
export interface AdoptionItemRow {
  item_code: string;
  rows: number;
  qty: number;
}
export interface AdoptionTierRow {
  tier: string;
  recommended: number;
  adopted: number;
  adoption_pct: number;
}
export interface AdoptionResponse {
  available: boolean;
  message?: string;
  start_date: string;
  end_date: string;
  route_code?: string | null;
  summary: AdoptionSummary | null;
  daily: AdoptionDailyPoint[];
  top_over_recommended: AdoptionItemRow[];
  top_missed: AdoptionItemRow[];
  by_tier: AdoptionTierRow[];
}

// ---------- Analytics: Upcoming Plan ----------
export interface UpcomingSummary {
  total_visits: number;
  total_qty: number;
  total_revenue: number | null;
  peak_day: { date: string; predicted_qty: number } | null;
  active_days: number;
}
export interface UpcomingDailyPoint {
  date: string;
  customers: number;
  routes: number;
  predicted_qty: number;
  est_revenue: number | null;
}
export interface UpcomingResponse {
  available: boolean;
  today: string;
  days: number;
  route_code?: string | null;
  summary: UpcomingSummary;
  daily: UpcomingDailyPoint[];
}
