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

export interface EmptyRouteCustomer {
  customer_code: string;
  customer_name: string;
  typical_items: { code: string; name: string }[];
}

export interface EmptyRouteDiagnosis {
  reason:
    | "no_plan"
    | "no_journey"
    | "no_van"
    | "all_new_customers"
    | "van_mismatch"
    | "mixed"
    | "engine_no_match";
  headline: string;
  detail: string;
  customers: EmptyRouteCustomer[];
}

export interface RetrieveResponse {
  success: boolean;
  date: string;
  total: number;
  data: RecommendationItem[];
  source: "store" | "generated";
  generated_routes: number;
  diagnosis?: EmptyRouteDiagnosis | null;
}

export interface FilterOptionsResponse {
  routes: string[];
  journey_counts: Record<string, number>;
  // Per-route diagnosis for routes that have planned customers but no stored
  // recommendations -- powers the route picker grid's empty-state message.
  route_diagnoses?: Record<string, EmptyRouteDiagnosis>;
}

// ---------- Analytics: Adoption ----------
export interface AdoptionSummary {
  // Decomposition of recommended volume/revenue:
  //   driven_*  + unsold_*  = recommended_*    (reconciles exactly)
  // driven  -> Σ min(rec, actual)  -- recs that converted (Tile 1)
  // unsold  -> Σ max(0, rec - act) -- recs that didn't convert (Tile 4)
  driven_volume: number;
  driven_revenue: number | null;
  unsold_volume: number;
  unsold_revenue: number | null;
  unsold_sku_count: number;
  recommended_volume: number;
  recommended_revenue: number | null;
  // Tile 3: Perfect picks; tolerance flows from backend to keep "within X%"
  // label in sync with the server-side definition.
  skus_perfect: number;
  perfect_pick_tolerance: number;
  // Tile 2 denominator + context bar + highlights
  skus_recommended: number;
  skus_adopted: number;
  skus_bought: number;
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
