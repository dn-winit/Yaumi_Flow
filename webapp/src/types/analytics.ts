export interface AnalysisResponse {
  success: boolean;
  analysis_type: string;
  data: Record<string, unknown>;
  cached: boolean;
}

export interface CacheStatsResponse {
  hits: number;
  misses: number;
  hit_rate: number;
  cached_entries: number;
}

export interface AnalyticsHealthResponse {
  available: boolean;
  provider: string;
  model: string;
  cache: CacheStatsResponse;
  prompts: string[];
}
