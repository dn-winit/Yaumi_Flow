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

/** Forecast accuracy as a signed % error. `<10%` great, `<25%` warn, else bad. */
export const accuracyTone = (pct: number | null | undefined): Tone => {
  if (pct == null || !Number.isFinite(pct)) return "neutral";
  const abs = Math.abs(pct);
  if (abs < 10) return "success";
  if (abs < 25) return "warning";
  return "danger";
};

/** Confidence score: 0..1, higher is better.
 *  Breakpoints come from format.ts so the badge and the "Risky items"
 *  KPI tile share one source of truth. */
import { AT_RISK_CONFIDENCE, STRONG_CONFIDENCE } from "./format";

export const confidenceTone = (value: number | null | undefined): Tone =>
  toneFromValue(value, [
    [AT_RISK_CONFIDENCE, "danger"],
    [STRONG_CONFIDENCE, "warning"],
    [1, "success"],
  ]);
