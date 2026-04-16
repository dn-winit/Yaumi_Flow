import { getClient } from "./client";
import type {
  AnalysisResponse,
  CacheStatsResponse,
  AnalyticsHealthResponse,
} from "@/types/analytics";
import type { LlmSummary } from "@/types/common";

const c = () => getClient("analytics");

export const analyticsApi = {
  getSummary: () => c().get<LlmSummary>("/summary").then((r) => r.data),

  analyzeCustomer: (data: Record<string, unknown>) =>
    c().post<AnalysisResponse>("/analyze/customer", data, { timeout: 180000 }).then((r) => r.data),

  analyzeRoute: (data: Record<string, unknown>) =>
    c().post<AnalysisResponse>("/analyze/route", data, { timeout: 180000 }).then((r) => r.data),

  analyzePlanning: (data: Record<string, unknown>) =>
    c().post<AnalysisResponse>("/analyze/planning", data, { timeout: 180000 }).then((r) => r.data),

  getHealth: () =>
    c().get<AnalyticsHealthResponse>("/health").then((r) => r.data),

  getCacheStats: () =>
    c().get<CacheStatsResponse>("/cache/stats").then((r) => r.data),

  clearCache: () =>
    c().post("/cache/clear").then((r) => r.data),
};
