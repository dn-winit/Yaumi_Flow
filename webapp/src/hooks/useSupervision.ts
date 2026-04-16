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
