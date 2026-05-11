import { useQuery } from "@tanstack/react-query";
import { supervisionApi } from "@/api/supervision";
import { tier } from "./refresh";

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
    ...tier("live"),
    refetchIntervalInBackground: false,
  });
  return {
    data,
    loading: isFetching,
    error: error ? String(error) : null,
    refetch,
    updatedAt: dataUpdatedAt,
  };
}

/**
 * Saved visits for a (route, date), used to hydrate already-visited
 * customers on mount. The query fires once per (route, date); a
 * fresh visit completion invalidates this key so the hydration view
 * stays in sync with the live state.
 */
export function useSavedVisits(routeCode: string, date: string) {
  return useQuery({
    queryKey: ["supervision-saved-visits", routeCode, date],
    queryFn: () => supervisionApi.getSavedVisits(routeCode, date),
    enabled: !!routeCode && !!date,
  });
}
