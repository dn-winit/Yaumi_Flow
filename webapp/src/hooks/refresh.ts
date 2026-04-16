/**
 * Shared React Query refresh cadence so every metric across the app stays
 * live-with-time. One source of truth — no per-hook magic numbers.
 *
 * Each tier pairs `staleTime` + `refetchInterval` with `refetchOnWindowFocus`
 * so the values update while the user watches AND when they return to a tab.
 *
 *   LIVE       45 s   — supervision live actuals, unplanned visits
 *   DASHBOARD  5 min  — dashboard KPIs + rolling business-wide aggregates
 *   WINDOWED   10 min — rolling-window analytics drawers (last 30 days);
 *                       the date params roll on every render so the window
 *                       naturally advances as the day changes
 *   STATIC     1 h    — catalogs that change rarely (item names, route codes,
 *                       filter options)
 *   PIPELINE   10 s   — short-lived admin pipeline state
 *   HEALTH     60 s   — service health checks
 */
const SECOND = 1000;
const MINUTE = 60 * SECOND;

export const REFRESH = {
  live:      { interval: 45 * SECOND,  stale: 30 * SECOND },
  dashboard: { interval: 5  * MINUTE,  stale: 5  * MINUTE },
  windowed:  { interval: 10 * MINUTE,  stale: 5  * MINUTE },
  static:    { interval: false as const, stale: 60 * MINUTE },
  pipeline:  { interval: 10 * SECOND,  stale: 5  * SECOND },
  health:    { interval: 60 * SECOND,  stale: 60 * SECOND },
} as const;

/**
 * Spread into `useQuery` options to apply a tier consistently.
 *   useQuery({ queryKey, queryFn, ...tier("windowed") })
 */
export function tier(name: keyof typeof REFRESH) {
  const t = REFRESH[name];
  return {
    staleTime: t.stale,
    refetchInterval: t.interval,
    refetchOnWindowFocus: true,
  };
}
