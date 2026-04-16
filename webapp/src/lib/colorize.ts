/**
 * Semantic tone helpers — replace ad-hoc threshold colouring in the UI.
 *
 * Consumers pass numeric values and receive a `Tone` that maps to a Badge
 * variant, icon colour, or any other token-driven styling. Keep the set of
 * helpers limited to genuinely reused thresholds; one-off mappings belong in
 * the calling site.
 */

export type Tone = "success" | "warning" | "danger" | "info" | "neutral";

/**
 * Generic threshold mapper.
 *
 * `thresholds` is an ascending list of `[breakpoint, tone]` pairs: the first
 * entry whose breakpoint is >= `value` wins. If no breakpoint matches (value
 * exceeds the last breakpoint) the last tone is returned.
 */
export function toneFromValue(
  value: number | null | undefined,
  thresholds: Array<[number, Tone]>,
): Tone {
  if (value == null || !Number.isFinite(value)) return "neutral";
  for (const [breakpoint, tone] of thresholds) {
    if (value <= breakpoint) return tone;
  }
  return thresholds[thresholds.length - 1]?.[1] ?? "neutral";
}

/** Churn probability: 0..1, lower is better. */
export const churnTone = (value: number | null | undefined): Tone =>
  toneFromValue(value, [
    [0.2, "success"],
    [0.5, "warning"],
    [1,   "danger"],
  ]);

/** Trend factor: 1.0 flat, >1 growing, <1 shrinking. */
export const trendTone = (factor: number | null | undefined): Tone => {
  if (factor == null || !Number.isFinite(factor)) return "neutral";
  if (factor > 1) return "success";
  if (factor < 1) return "danger";
  return "neutral";
};

/** Forecast accuracy as a signed % error. `<10%` great, `<25%` warn, else bad. */
export const accuracyTone = (pct: number | null | undefined): Tone => {
  if (pct == null || !Number.isFinite(pct)) return "neutral";
  const abs = Math.abs(pct);
  if (abs < 10) return "success";
  if (abs < 25) return "warning";
  return "danger";
};

/** Confidence score: 0..1, higher is better. */
export const confidenceTone = (value: number | null | undefined): Tone =>
  toneFromValue(value, [
    [0.7, "danger"],
    [0.9, "warning"],
    [1,   "success"],
  ]);

/** Variance sign: positive = success (over), negative = danger (under). */
export const varianceTone = (value: number | null | undefined): Tone => {
  if (value == null || !Number.isFinite(value)) return "neutral";
  if (value > 0) return "success";
  if (value < 0) return "danger";
  return "neutral";
};

/**
 * Pattern-quality score: 0..1, higher is more regular. Uses `info` for weak
 * patterns to avoid crying wolf — they're not bad, just less informative.
 */
export const patternQualityTone = (value: number | null | undefined): Tone => {
  if (value == null || !Number.isFinite(value)) return "neutral";
  if (value >= 0.7) return "success";
  if (value >= 0.4) return "warning";
  return "info";
};
