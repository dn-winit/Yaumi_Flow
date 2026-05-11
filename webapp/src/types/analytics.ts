export interface AnalysisResponse {
  success: boolean;
  analysis_type: string;
  data: Record<string, unknown>;
}

export interface CacheStatsResponse {
  hits: number;
  misses: number;
  hit_rate: number;
  cached_entries: number;
}
