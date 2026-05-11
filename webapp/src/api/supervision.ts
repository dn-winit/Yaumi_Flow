import { getClient } from "./client";
import type {
  SessionResponse,
  VisitResponse,
  UnplannedVisitsResponse,
  SavedVisitsResponse,
} from "@/types/supervision";

const c = () => getClient("supervision");

/**
 * Frontend surface for the supervision microservice. Mirrors the
 * server-side route list exactly: session lifecycle (init / visit)
 * plus the unplanned-visits poll. Each visit auto-persists to the
 * YaumiAIML supervision tables -- there is no separate save call.
 */
export const supervisionApi = {
  initSession: (route_code: string, date: string, recommendations: Record<string, unknown>[]) =>
    c().post<SessionResponse>("/session/initialize", { route_code, date, recommendations }).then((r) => r.data),

  // Actuals are fetched server-side from YaumiLive -- the client only
  // tells the service which customer to mark visited.
  processVisit: (session_id: string, customer_code: string) =>
    c().post<VisitResponse>("/session/visit", { session_id, customer_code }).then((r) => r.data),

  getUnplannedVisits: (session_id: string) =>
    c().get<UnplannedVisitsResponse>(`/session/unplanned/${session_id}`).then((r) => r.data),

  // Hydrates the live UI on mount: returns visit-result-shaped payloads
  // for every customer that already has a row in yf_supervision_*.
  getSavedVisits: (route_code: string, date: string) =>
    c()
      .get<SavedVisitsResponse>("/session/saved", { params: { route_code, date } })
      .then((r) => r.data),

  // LLM payload persistence. Each call is fire-and-forget; the server
  // runs the actual DB write as a BackgroundTask so the UI never
  // blocks on warehouse latency. ``content`` is the full analytics
  // response, JSON-stringified -- stored as-is so an upstream schema
  // change in the LLM payload doesn't require a DB migration.
  saveBriefing: (session_id: string, customer_code: string, content: string) =>
    c().post("/session/briefing", { session_id, customer_code, content }).then((r) => r.data),

  saveCustomerAnalysis: (session_id: string, customer_code: string, content: string) =>
    c().post("/session/customer-analysis", { session_id, customer_code, content }).then((r) => r.data),

  saveRouteAnalysis: (session_id: string, content: string) =>
    c().post("/session/route-analysis", { session_id, content }).then((r) => r.data),
};
