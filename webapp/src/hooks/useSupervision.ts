import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { supervisionApi } from "@/api/supervision";
import { REFRESH, tier } from "./refresh";

export function useSupervisionSummary() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["supervision-summary"],
    queryFn: () => supervisionApi.getSummary(),
    ...tier("dashboard"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useSessionInit() {
  const m = useMutation({
    mutationFn: (p: { route: string; date: string; recs: Record<string, unknown>[] }) =>
      supervisionApi.initSession(p.route, p.date, p.recs),
  });
  return {
    execute: (route: string, date: string, recs: Record<string, unknown>[]) =>
      m.mutate({ route, date, recs }),
    loading: m.isPending,
    error: m.error ? String(m.error) : null,
    session: m.data?.session,
  };
}

export function useProcessVisit() {
  const m = useMutation({
    mutationFn: (p: { sessionId: string; customer: string }) =>
      supervisionApi.processVisit(p.sessionId, p.customer),
  });
  return {
    execute: (sessionId: string, customer: string) => m.mutate({ sessionId, customer }),
    loading: m.isPending,
    error: m.error ? String(m.error) : null,
    result: m.data,
  };
}

export function useRouteScore(sessionId: string) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["route-score", sessionId],
    queryFn: () => supervisionApi.getRouteScore(sessionId),
    enabled: !!sessionId,
    ...tier("pipeline"),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

/**
 * Unplanned drop-ins for the session's route/date.
 *
 * Polled at the LIVE tier. The server caches the underlying live query for
 * 60 s, so fleet-wide polling hits the DB at most once per minute per route.
 * Background tabs pause automatically so we don't ping while hidden.
 */
export function useUnplannedVisits(sessionId: string) {
  const { data, isFetching, error, refetch, dataUpdatedAt } = useQuery({
    queryKey: ["supervision-unplanned", sessionId],
    queryFn: () => supervisionApi.getUnplannedVisits(sessionId),
    enabled: !!sessionId,
    refetchInterval: REFRESH.live.interval,
    refetchIntervalInBackground: false,
    staleTime: REFRESH.live.stale,
    refetchOnWindowFocus: true,
  });
  return {
    data,
    loading: isFetching,
    error: error ? String(error) : null,
    refetch,
    updatedAt: dataUpdatedAt,
  };
}

export function useReviewDates(routeCode?: string) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["review-dates", routeCode ?? ""],
    queryFn: () => supervisionApi.listDates(routeCode),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useReviewSessions(date?: string) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ["review-sessions", date ?? ""],
    queryFn: () => supervisionApi.listSessions(date),
  });
  return { data, loading: isLoading, error: error ? String(error) : null, refetch };
}

export function useLoadReview() {
  const qc = useQueryClient();
  const m = useMutation({
    mutationFn: (p: { route: string; date: string }) =>
      supervisionApi.loadReview(p.route, p.date),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["supervision-summary"] });
    },
  });
  return {
    execute: (route: string, date: string) => m.mutate({ route, date }),
    loading: m.isPending,
    error: m.error ? String(m.error) : null,
    session: m.data?.session,
  };
}
