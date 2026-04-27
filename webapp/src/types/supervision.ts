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
  };
}

export interface UnplannedVisitor {
  customer_code: string;
  customer_name?: string;
  total_qty: number;
  items: { item_code: string; qty: number }[];
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
