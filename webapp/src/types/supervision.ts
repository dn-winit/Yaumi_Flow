/** One downstream-recipient line inside a ``RedistributionGroup``.
 *
 *  ``quantity`` is always a positive magnitude; the signed semantics
 *  live in ``direction``:
 *    - ``"add"``    -> units flowed TO the recipient (surplus
 *                      reallocated from the just-visited customer).
 *    - ``"reduce"`` -> recipient's planned share was cut (encroachment
 *                      because the just-visited customer over-bought).
 *
 *  This is a string-literal union, not ``string``, so TypeScript forces
 *  every consumer to handle both cases explicitly. */
export interface RedistributionEntry {
  to: string;
  toName: string;
  quantity: number;
  direction: "add" | "reduce";
}

/** Per-SKU grouping: each item that got redistributed is one group.
 *
 *  ``keptOnTruck`` carries the leftover surplus that the engine could
 *  NOT place with a downstream recipient: an under-sold item with no
 *  takers stays on the van. Server defaults this to 0; the UI surfaces
 *  the number so supervisors see explicitly when stock is sitting in
 *  the truck rather than being redistributed away. */
export interface RedistributionGroup {
  itemCode: string;
  itemName: string;
  entries: RedistributionEntry[];
  keptOnTruck: number;
}

/** Wire-shaped redistribution payload the server emits for EVERY visit
 *  (planned or drop-in). The field is non-optional on every visit
 *  carrier so the panel can mount unconditionally and render its
 *  per-item entries (or an empty body) without a null guard. */
export interface RedistributionView {
  groups: RedistributionGroup[];
}

/** Cumulative session-level visit aggregates. Server-pushed by both
 *  ``/visit`` (after each visit) and ``/saved`` (on hydrate) so the
 *  client never re-sums the visits map. */
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

/** Pre-shaped customer payload from ``Session.summary().customers_grouped``.
 *  Items are filtered to ``recommended_qty > 0`` and customers with no
 *  surviving items are dropped, matching what the live UI used to
 *  compute on the client. */
export interface SessionCustomerGrouped {
  customer_code: string;
  customer_name: string;
  items: Record<string, unknown>[];
}

/** Per-customer tile stats, server-pre-computed. ``visited`` reflects
 *  in-session state at the time the session was emitted; live updates
 *  lift it off the local visits map. */
export interface SessionCustomerTile {
  customer_code: string;
  customer_name: string;
  unique_skus: number;
  total_units: number;
  visited: boolean;
}

/** Typed session payload the server returns from ``/session/initialize``.
 *  Every aggregate the UI reads is pre-computed -- the client never
 *  re-sums or re-groups anything below this boundary. */
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

/** Typed visit-result payload. Returned by ``/session/visit`` and
 *  consumed by the live tab's ``handleVisitComplete``. Every numeric
 *  here is server-computed so the client renders verbatim. */
export interface VisitResultPayload {
  score: VisitScore;
  /** Live-fetched per-item actuals for the visited customer, keyed by
   *  ItemCode. Pulled from data_import inside the request handler so
   *  the client never supplies them. */
  actualSales: Record<string, number>;
  /** Rec-fulfilled total = sum of ``min(rec, act)`` per planned item. */
  actualQty: number;
  /** Sum of recommended quantities for the customer's planned items. */
  recommendedQty: number;
  /** Items the customer bought that were NOT in the planned list.
   *  Awareness-only context (no score impact); sorted desc by qty. */
  alsoBought: AlsoBoughtRow[];
  redistributions: RedistributionView;
  /** Cumulative session-level visit aggregates INCLUDING this latest
   *  visit. Same shape ``Session.summary().visit_totals`` emits, so
   *  the client drops it directly into its visit-totals state slot. */
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
  /** Server-computed tile fields. Server pre-counts unique SKUs and
   *  flags ``live_visited`` so the UI grid maps the customer to a
   *  CustomerStat without iterating items. */
  unique_skus: number;
  live_visited: boolean;
  /** Always present on the wire. The drop-in panel always renders;
   *  when the customer's van consumption couldn't be matched to a
   *  downstream planned recipient, ``groups`` is simply empty. */
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
  /** Off-plan items the customer invoiced -- persisted in
   *  ``yf_supervision_items`` with ``original_recommended_qty=0`` so a
   *  page reload hydrates the same "Also bought" chip strip the live
   *  ``/visit`` response surfaces. Empty array when no off-plan rows. */
  alsoBought: AlsoBoughtRow[];
}

export interface SavedVisitsResponse {
  available: boolean;
  session_id?: string | null;
  visits: Record<string, SavedVisit>;
  // Route-level LLM review for the (route, date), if any.
  routeAnalysis?: string | null;
  visit_totals: SessionVisitTotals;
  // Pre-visit briefings keyed by customer_code. Populated for every
  // planned customer the cron has briefed -- visited OR not-yet-visited
  // -- so the briefing modal can render any planned customer without a
  // fresh LLM round-trip. Drop-in (unplanned) customers are not present.
  briefings?: Record<string, string>;
}
