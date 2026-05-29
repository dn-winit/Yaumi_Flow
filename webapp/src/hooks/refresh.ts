/** Shared React Query refresh cadences. Tiers: live (45s), dashboard (5m),
 *  windowed (10m), static (1h), pipeline (10s), health (60s). */
const SECOND = 1000;
const MINUTE = 60 * SECOND;

export const REFRESH = {
  live: { interval: 45 * SECOND, stale: 30 * SECOND },
  dashboard: { interval: 5 * MINUTE, stale: 5 * MINUTE },
  windowed: { interval: 10 * MINUTE, stale: 5 * MINUTE },
  static: { interval: false as const, stale: 60 * MINUTE },
  pipeline: { interval: 10 * SECOND, stale: 5 * SECOND },
  health: { interval: 60 * SECOND, stale: 60 * SECOND },
} as const;

/** Spread into useQuery options: `useQuery({ ..., ...tier("windowed") })`. */
export function tier(name: keyof typeof REFRESH) {
  const t = REFRESH[name];
  return {
    staleTime: t.stale,
    refetchInterval: t.interval,
    refetchOnWindowFocus: true,
  };
}
