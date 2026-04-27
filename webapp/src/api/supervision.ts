import { getClient } from "./client";
import type { SessionResponse, VisitResponse, UnplannedVisitsResponse } from "@/types/supervision";

const c = () => getClient("supervision");

/**
 * Frontend surface for the supervision microservice. Mirrors the
 * server-side route list exactly: session lifecycle (init / visit /
 * save) plus the unplanned-visits poll.
 */
export const supervisionApi = {
  initSession: (route_code: string, date: string, recommendations: Record<string, unknown>[]) =>
    c().post<SessionResponse>("/session/initialize", { route_code, date, recommendations }).then((r) => r.data),

  // Actuals are fetched server-side from YaumiLive -- the client only
  // tells the service which customer to mark visited.
  processVisit: (session_id: string, customer_code: string) =>
    c().post<VisitResponse>("/session/visit", { session_id, customer_code }).then((r) => r.data),

  saveActiveSession: (session_id: string) =>
    c().post("/session/save-active", null, { params: { session_id } }).then((r) => r.data),

  getUnplannedVisits: (session_id: string) =>
    c().get<UnplannedVisitsResponse>(`/session/unplanned/${session_id}`).then((r) => r.data),
};
