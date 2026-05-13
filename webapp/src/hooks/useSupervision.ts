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
 * customers on mount. Polled at the LIVE tier so the saved-totals row
 * stays aligned with the 60s auto-reconciler cadence -- and the cache
 * picks up any out-of-band write (e.g. another supervisor session
 * touching the same route) within one polling window. The live ``/visit``
 * handler also invalidates this key directly so fresh writes land
 * immediately without waiting for the next poll.
 */
export function useSavedVisits(routeCode: string, date: string) {
  return useQuery({
    queryKey: ["supervision-saved-visits", routeCode, date],
    queryFn: () => supervisionApi.getSavedVisits(routeCode, date),
    enabled: !!routeCode && !!date,
    ...tier("live"),
    refetchIntervalInBackground: false,
  });
}
