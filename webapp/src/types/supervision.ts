export interface SessionResponse {
  success: boolean;
  session: Record<string, unknown>;
}

export interface VisitResponse {
  success: boolean;
  visit: {
    customerCode: string;
    score: { score: number; coverage: number; accuracy: number };
    unsoldItems: Record<string, number>;
    redistributions: { from: string; to: string; itemCode: string; quantity: number }[];
    adjustments: Record<string, Record<string, number>>;
    /** Live-fetched per-item actuals for the visited customer, keyed by
     *  ItemCode. Pulled from data_import inside the request handler so
     *  the client never supplies them. */
    actualSales: Record<string, number>;
    /** Items the customer bought that were NOT in the planned list.
     *  Awareness-only context (no score impact); sorted desc by qty. */
    alsoBought: { item_code: string; qty: number }[];
    /** ``true`` when the actuals came from the live YaumiLive cut-through
     *  (vs a fallback / stub). Always ``true`` in normal flow. */
    actualFetchedFromLive: boolean;
    /** Rec-fulfilled total = sum of ``min(rec, act)`` per planned item.
     *  Server-computed; the client renders verbatim. */
    actualQty: number;
    /** Sum of recommended quantities for the customer's planned items. */
    recommendedQty: number;
    /** Cumulative session-level visit aggregates including this latest
     *  visit. Same shape ``Session.summary().visit_totals`` emits, so
     *  the client drops it directly into its visit-totals state slot. */
    sessionTotals: VisitTotals;
  };
}

export interface UnplannedVisitor {
  customer_code: string;
  customer_name?: string;
  total_qty: number;
  items: { item_code: string; qty: number }[];
  /** Server-computed tile fields. Server pre-counts unique SKUs and
   *  flags ``live_visited`` so the UI grid maps the customer to a
   *  CustomerStat without iterating items. */
  unique_skus: number;
  live_visited: boolean;
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
  score: { score: number; coverage: number; accuracy: number };
  actualSales: Record<string, number>;
  totalActual: number;
  totalRecommended: number;
  // LLM payloads previously saved for this customer. Carried as
  // opaque strings -- the analytics layer JSON-parses on render.
  preVisitBriefing?: string | null;
  customerAnalysis?: string | null;
}

/** Cumulative visit aggregates the live tile row reads. Server-pushed
 *  by both ``/visit`` (after each visit) and ``/saved`` (on hydrate)
 *  so the client never re-sums the visits map. */
export interface VisitTotals {
  visited_count: number;
  total_actual: number;
  total_recommended: number;
  avg_score: number | null;
}

export interface SavedVisitsResponse {
  available: boolean;
  session_id?: string | null;
  visits: Record<string, SavedVisit>;
  // Route-level LLM review for the (route, date), if any.
  routeAnalysis?: string | null;
  visit_totals: VisitTotals;
}

/** Pre-shaped customer payload from ``Session.summary().customers_grouped``.
 *  Items are filtered to ``recommended_qty > 0`` and customers with no
 *  surviving items are dropped, matching what the live UI used to
 *  compute on the client. */
export interface SessionCustomerGrouped {
  customer_code: string;
  customer_name: string;
  items: Record<string, unknown>[];
}

/** Per-customer tile stats, server-pre-computed.  ``visited`` reflects
 *  in-session state at the time the session was emitted; live updates
 *  lift it off the local visits map. */
export interface SessionCustomerTile {
  customer_code: string;
  customer_name: string;
  unique_skus: number;
  total_units: number;
  visited: boolean;
}

/** Static (non-visit-dependent) totals from the recommendations. */
export interface RecommendationTotals {
  items_count: number;
  total_units: number;
  customers_count: number;
}
