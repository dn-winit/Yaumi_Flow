/** Semantic tone helpers; reused threshold mappers only. One-offs belong at the call site. */

export type Tone = "success" | "warning" | "danger" | "info" | "neutral";

/** Threshold mapper; ascending `[breakpoint, tone]` -- first match wins, falls through to last. */
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

/** Confidence score 0..1; breakpoints sourced from format.ts. */
import { AT_RISK_CONFIDENCE, STRONG_CONFIDENCE } from "./format";

export const confidenceTone = (value: number | null | undefined): Tone =>
  toneFromValue(value, [
    [AT_RISK_CONFIDENCE, "danger"],
    [STRONG_CONFIDENCE, "warning"],
    [1, "success"],
  ]);
