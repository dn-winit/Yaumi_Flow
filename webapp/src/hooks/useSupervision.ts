import { useQuery } from "@tanstack/react-query";
import { supervisionApi } from "@/api/supervision";
import { tier } from "./refresh";

/** Unplanned drop-ins for the session route/date; polled at LIVE tier, paused in background. */
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

/** Saved visits for (route, date); hydrates visited customers on mount.
 *  LIVE tier aligns with the 60s auto-reconciler; /visit also invalidates this key directly. */
export function useSavedVisits(routeCode: string, date: string) {
  return useQuery({
    queryKey: ["supervision-saved-visits", routeCode, date],
    queryFn: () => supervisionApi.getSavedVisits(routeCode, date),
    enabled: !!routeCode && !!date,
    ...tier("live"),
    refetchIntervalInBackground: false,
  });
}
