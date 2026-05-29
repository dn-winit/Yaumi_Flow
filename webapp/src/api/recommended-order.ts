import { getClient, TIMEOUTS } from "./client";
import type {
  GenerateRequest,
  GenerateResponse,
  RetrieveRequest,
  RetrieveResponse,
  FilterOptionsResponse,
  AdoptionResponse,
  UpcomingResponse,
} from "@/types/recommended-order";
import type { RecommendationSummary } from "@/types/common";

const c = () => getClient("recommendedOrder");

/** Recommended-order service surface. */
export const recommendedOrderApi = {
  getSummary: () =>
    c()
      .get<RecommendationSummary>("/summary")
      .then((r) => r.data),

  generate: (data: GenerateRequest) =>
    c()
      .post<GenerateResponse>("/generate", data, { timeout: TIMEOUTS.heavy })
      .then((r) => r.data),

  getRecommendations: (data: RetrieveRequest) =>
    c()
      .post<RetrieveResponse>("/get", data, { timeout: TIMEOUTS.heavy })
      .then((r) => r.data),

  getFilterOptions: (date?: string) =>
    c()
      .get<FilterOptionsResponse>("/filter-options", { params: date ? { date } : {} })
      .then((r) => r.data),

  getAdoption: (params: {
    start_date: string;
    end_date: string;
    route_code?: string;
    category_codes?: string[];
    item_codes?: string[];
  }) =>
    c()
      .get<AdoptionResponse>("/analytics/adoption", { params })
      .then((r) => r.data),

  getUpcoming: (params: { days?: number; route_code?: string }) =>
    c()
      .get<UpcomingResponse>("/analytics/upcoming", { params })
      .then((r) => r.data),
};
