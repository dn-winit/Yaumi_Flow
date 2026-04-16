import { getClient } from "./client";
import type {
  GenerateRequest,
  GenerateResponse,
  RetrieveRequest,
  RetrieveResponse,
  FilterOptionsResponse,
  OrderHealthResponse,
  AdoptionResponse,
  UpcomingResponse,
} from "@/types/recommended-order";
import type { RecommendationSummary } from "@/types/common";

const c = () => getClient("recommendedOrder");

export const recommendedOrderApi = {
  getSummary: () => c().get<RecommendationSummary>("/summary").then((r) => r.data),

  generate: (data: GenerateRequest) =>
    c().post<GenerateResponse>("/generate", data, { timeout: 180000 }).then((r) => r.data),

  getRecommendations: (data: RetrieveRequest) =>
    c().post<RetrieveResponse>("/get", data, { timeout: 180000 }).then((r) => r.data),

  getFilterOptions: () =>
    c().get<FilterOptionsResponse>("/filter-options").then((r) => r.data),

  getAdoption: (params: { start_date: string; end_date: string; route_code?: string }) =>
    c().get<AdoptionResponse>("/analytics/adoption", { params }).then((r) => r.data),

  getUpcoming: (params: { days?: number; route_code?: string }) =>
    c().get<UpcomingResponse>("/analytics/upcoming", { params }).then((r) => r.data),

  refreshData: () => c().post("/refresh-data").then((r) => r.data),

  getHealth: () => c().get<OrderHealthResponse>("/health").then((r) => r.data),
};
