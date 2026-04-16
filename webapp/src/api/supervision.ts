import { getClient } from "./client";
import type {
  SessionResponse,
  VisitResponse,
  ScoreResponse,
  RouteScoreResponse,
  ReviewResponse,
  SessionListItem,
  MethodologyResponse,
  SupervisionHealthResponse,
  UnplannedVisitsResponse,
} from "@/types/supervision";
import type { SupervisionSummary } from "@/types/common";

const c = () => getClient("supervision");

export const supervisionApi = {
  getSummary: () => c().get<SupervisionSummary>("/summary").then((r) => r.data),

  // Session
  initSession: (route_code: string, date: string, recommendations: Record<string, unknown>[]) =>
    c().post<SessionResponse>("/session/initialize", { route_code, date, recommendations }).then((r) => r.data),

  // Actuals are fetched server-side from YaumiLive -- client only tells
  // which customer to mark visited.
  processVisit: (session_id: string, customer_code: string) =>
    c().post<VisitResponse>("/session/visit", { session_id, customer_code }).then((r) => r.data),

  updateActuals: (session_id: string, route_code: string, date: string, customer_code: string, actuals: Record<string, number>) =>
    c().post<ScoreResponse>("/session/update-actuals", { session_id, route_code, date, customer_code, actuals }).then((r) => r.data),

  saveActiveSession: (session_id: string) =>
    c().post(`/session/save-active?session_id=${session_id}`).then((r) => r.data),

  getSessionSummary: (session_id: string) =>
    c().get(`/session/summary/${session_id}`).then((r) => r.data),

  getFullSession: (session_id: string) =>
    c().get(`/session/full/${session_id}`).then((r) => r.data),

  getRouteScore: (session_id: string) =>
    c().get<RouteScoreResponse>(`/session/route-score/${session_id}`).then((r) => r.data),

  getUnplannedVisits: (session_id: string) =>
    c().get<UnplannedVisitsResponse>(`/session/unplanned/${session_id}`).then((r) => r.data),

  // Review
  loadReview: (route_code: string, date: string) =>
    c().post<ReviewResponse>("/review/load", { route_code, date }).then((r) => r.data),

  checkReviewExists: (route_code: string, date: string) =>
    c().get("/review/exists", { params: { route_code, date } }).then((r) => r.data),

  listDates: (route_code?: string) =>
    c().get<{ success: boolean; dates: string[] }>("/review/dates", { params: { route_code } }).then((r) => r.data),

  listSessions: (date?: string) =>
    c().get<{ success: boolean; sessions: SessionListItem[] }>("/review/sessions", { params: { date } }).then((r) => r.data),

  deleteSession: (route_code: string, date: string) =>
    c().delete("/review/delete", { params: { route_code, date } }).then((r) => r.data),

  // Scoring
  scoreCustomer: (items: Record<string, unknown>[]) =>
    c().post<ScoreResponse>("/scoring/customer", { items }).then((r) => r.data),

  scoreRoute: (customers: Record<string, unknown>[]) =>
    c().post<RouteScoreResponse>("/scoring/route", { customers }).then((r) => r.data),

  getMethodology: () =>
    c().get<MethodologyResponse>("/scoring/methodology").then((r) => r.data),

  getHealth: () =>
    c().get<SupervisionHealthResponse>("/health").then((r) => r.data),
};
