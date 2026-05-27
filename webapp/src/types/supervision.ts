/** One downstream-recipient line inside a RedistributionGroup. `quantity` is always
 *  positive; sign lives in `direction` ("add" = surplus flowed to recipient,
 *  "reduce" = recipient's planned share was cut due to encroachment). */
export interface RedistributionEntry {
  to: string;
  toName: string;
  quantity: number;
  direction: "add" | "reduce";
}

/** Per-SKU grouping; one group per redistributed item.
 *  `keptOnTruck` = surplus the engine could not place downstream (stays on the van). */
export interface RedistributionGroup {
  itemCode: string;
  itemName: string;
  entries: RedistributionEntry[];
  keptOnTruck: number;
}

/** Redistribution payload emitted for every visit; non-optional so the panel mounts
 *  unconditionally and renders an empty body when there's nothing to show. */
export interface RedistributionView {
  groups: RedistributionGroup[];
}

/** Cumulative session-level visit aggregates -- server-pushed by /visit and /saved. */
export interface SessionVisitTotals {
  visited_count: number;
  total_actual: number;
  total_recommended: number;
  avg_score: number | null;
  unplanned_visited_count: number;
}

/** Static (non-visit-dependent) totals from the recommendations. */
export interface SessionRecommendationTotals {
  items_count: number;
  total_units: number;
  customers_count: number;
}

/** Pre-shaped customer payload; items filtered to recommended_qty>0, empty customers dropped. */
export interface SessionCustomerGrouped {
  customer_code: string;
  customer_name: string;
  items: Record<string, unknown>[];
}

/** Per-customer tile stats; `visited` reflects session state at emit time. */
export interface SessionCustomerTile {
  customer_code: string;
  customer_name: string;
  unique_skus: number;
  total_units: number;
  visited: boolean;
}

/** Session payload from /session/initialize; every aggregate is pre-computed server-side. */
export interface SessionSummary {
  sessionId: string;
  routeCode: string;
  date: string;
  status: string;
  plannedCustomers: number;
  plannedVisitedCustomers: number;
  unplannedVisitedCustomers: number;
  totalCustomers: number;
  customers_grouped: SessionCustomerGrouped[];
  customer_tiles: SessionCustomerTile[];
  recommendation_totals: SessionRecommendationTotals;
  visit_totals: SessionVisitTotals;
}

export interface VisitScore {
  score: number;
  coverage: number;
  accuracy: number;
}

export interface AlsoBoughtRow {
  item_code: string;
  qty: number;
}

/** Visit-result payload from /session/visit; all numerics are server-computed. */
export interface VisitResultPayload {
  score: VisitScore;
  /** Live actuals for the visited customer, keyed by ItemCode (from data_import). */
  actualSales: Record<string, number>;
  /** Rec-fulfilled total = sum of min(rec, act) per planned item. */
  actualQty: number;
  /** Sum of recommended quantities for the customer's planned items. */
  recommendedQty: number;
  /** Off-plan items the customer bought; awareness-only, no score impact. Sorted desc. */
  alsoBought: AlsoBoughtRow[];
  redistributions: RedistributionView;
  /** Session-level visit aggregates INCLUDING this latest visit. */
  sessionTotals: SessionVisitTotals;
}

export interface SessionResponse {
  success: boolean;
  session: SessionSummary;
}

export interface VisitResponse {
  success: boolean;
  visit: VisitResultPayload;
}

export interface UnplannedVisitor {
  customer_code: string;
  customer_name?: string;
  total_qty: number;
  items: { item_code: string; qty: number }[];
  /** Pre-counted SKUs + visited flag so the grid skips per-item iteration. */
  unique_skus: number;
  live_visited: boolean;
  /** Always present; `groups` may be empty when no downstream recipient was found. */
  redistributions: RedistributionView;
}

export interface UnplannedVisitsResponse {
  success: boolean;
  error?: string;
  route_code: string;
  date: string;
  planned_count: number;
  live_count: number;
  unplanned_count: number;
  planned_visited_codes: string[];
  customers: UnplannedVisitor[];
}

export interface SavedVisit {
  score: VisitScore;
  actualSales: Record<string, number>;
  totalActual: number;
  totalRecommended: number;
  preVisitBriefing?: string | null;
  customerAnalysis?: string | null;
  redistributions: RedistributionView;
  /** Off-plan invoiced items, persisted with original_recommended_qty=0 so reload
   *  hydrates the same "Also bought" strip as the live /visit response. */
  alsoBought: AlsoBoughtRow[];
}

export interface SavedVisitsResponse {
  available: boolean;
  session_id?: string | null;
  visits: Record<string, SavedVisit>;
  // Route-level LLM review for the (route, date), if any.
  routeAnalysis?: string | null;
  visit_totals: SessionVisitTotals;
  // Pre-visit briefings keyed by customer_code for every planned customer (visited
  // or not). Drop-in customers are not present.
  briefings?: Record<string, string>;
}
