import { getClient } from "./client";
import type {
  SessionResponse,
  VisitResponse,
  UnplannedVisitsResponse,
  SavedVisitsResponse,
} from "@/types/supervision";

const c = () => getClient("supervision");

/** Supervision microservice surface; each visit auto-persists (no separate save). */
export const supervisionApi = {
  initSession: (
    route_code: string,
    date: string,
    recommendations: Record<string, unknown>[] = [],
  ) =>
    c()
      .post<SessionResponse>("/session/initialize", { route_code, date, recommendations })
      .then((r) => r.data),

  // Actuals are fetched server-side from YaumiLive -- the client only
  // tells the service which customer to mark visited.
  processVisit: (session_id: string, customer_code: string) =>
    c()
      .post<VisitResponse>("/session/visit", { session_id, customer_code })
      .then((r) => r.data),

  getUnplannedVisits: (session_id: string) =>
    c()
      .get<UnplannedVisitsResponse>(`/session/unplanned/${session_id}`)
      .then((r) => r.data),

  // Hydrates live UI on mount + 45s poll; skips the heavy redistribution replay.
  getSavedVisits: (route_code: string, date: string) =>
    c()
      .get<SavedVisitsResponse>("/session/saved", { params: { route_code, date } })
      .then((r) => r.data),

  getCustomerRedistribution: (route_code: string, date: string, customer_code: string) =>
    c()
      .get<{ available: boolean; redistributions?: unknown }>(
        `/session/redistribution/${route_code}/${date}/${customer_code}`,
      )
      .then((r) => r.data),

  // LLM analyses are no longer persisted server-side -- they are generated
  // on-demand by the webapp directly against llm_analytics. The save*
  // helpers (saveBriefing / saveCustomerAnalysis / saveRouteAnalysis) and
  // their /session/briefing, /session/customer-analysis, /session/route-analysis
  // endpoints have been removed end-to-end.
};
