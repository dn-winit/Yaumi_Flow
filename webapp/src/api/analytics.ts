import { getClient, TIMEOUTS } from "./client";
import type { AnalysisResponse, CacheStatsResponse } from "@/types/analytics";

const c = () => getClient("analytics");

/** LLM analytics surface; every call hits the LLM so all use the `heavy` timeout. */
export const analyticsApi = {
  analyzeCustomer: (data: Record<string, unknown>) =>
    c().post<AnalysisResponse>("/analyze/customer", data, { timeout: TIMEOUTS.heavy }).then((r) => r.data),

  analyzeRoute: (data: Record<string, unknown>) =>
    c().post<AnalysisResponse>("/analyze/route", data, { timeout: TIMEOUTS.heavy }).then((r) => r.data),

  preVisitBriefing: (data: {
    customer_code: string;
    customer_name: string;
    route_code: string;
    date: string;
    items: Record<string, unknown>[];
  }) =>
    c().post<AnalysisResponse>("/analyze/pre-visit", data, { timeout: TIMEOUTS.heavy }).then((r) => r.data),

  getCacheStats: () =>
    c().get<CacheStatsResponse>("/cache/stats").then((r) => r.data),

  clearCache: () =>
    c().post("/cache/clear").then((r) => r.data),
};
