/**
 * Shared number / currency / date formatting helpers.
 * Single source of truth -- imported by every page that renders metrics.
 */

export function toNum(v: unknown): number | null {
  if (v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

const MISSING = "--";

export function fmtNum(v: unknown, digits = 0): string {
  const n = toNum(v);
  if (n == null) return MISSING;
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function fmtCurrency(v: unknown): string {
  const n = toNum(v);
  if (n == null) return MISSING;
  const abs = Math.abs(n);
  if (abs >= 1_000_000) return `AED ${(n / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `AED ${(n / 1_000).toFixed(1)}K`;
  return `AED ${n.toFixed(2)}`;
}

/** Format a percentage delta as e.g. "+12.3%" / "-4.5%" with tone classification. */
export function fmtDelta(pct: number | null | undefined): { text: string; tone: "up" | "down" | "flat" } {
  if (pct == null) return { text: "no baseline", tone: "flat" };
  const sign = pct > 0 ? "+" : "";
  const tone = pct > 0.5 ? "up" : pct < -0.5 ? "down" : "flat";
  return { text: `${sign}${pct.toFixed(1)}%`, tone };
}

/**
 * Shared thresholds used across score displays.
 * - GOOD_SCORE_THRESHOLD: percentage cut-off above which a score is treated as
 *   "good" (green/up trend). Used by dashboard KPI tiles, live-session metrics
 *   and accuracy drawers so the visual language stays consistent.
 * - AT_RISK_CONFIDENCE: cycle-confidence floor below which a customer is
 *   surfaced as "at risk" of churn in the risk panels.
 */
export const GOOD_SCORE_THRESHOLD = 75;
export const AT_RISK_CONFIDENCE = 0.7;

/**
 * Van-load accuracy thresholds used on the Last-30-Days drawer.
 * Kept here so every tile + tooltip reads from the same place.
 *  - TOLERANCE_PCT: a day is "on target" when |predicted - actual| / actual is
 *    within this fraction. 20% mirrors the supervision perfect-zone ±20% band.
 *  - BIAS_GOOD_PCT / BIAS_WARN_PCT: van-load bias tone breakpoints in percent.
 *  - LEAKAGE_SHARE_WARN: lost-sales or dead-weight above this share of actuals
 *    is flagged as danger.
 */
export const TOLERANCE_PCT = 0.2;
export const BIAS_GOOD_PCT = 5;
export const BIAS_WARN_PCT = 15;
export const LEAKAGE_SHARE_WARN = 0.05;

/**
 * Recommendation-adoption thresholds (used in the Last-30-Days drawer).
 *  - HIT_RATE_GOOD: green when share of recs that sold >= this %.
 *  - COVERAGE_GOOD: green when share of customer sales captured by recs >= this %.
 *  - TREND_STEP_PP: percentage-point change that counts as improving/declining;
 *    smaller deltas are shown as "stable".
 */
export const HIT_RATE_GOOD = 50;
export const COVERAGE_GOOD = 50;
export const TREND_STEP_PP = 2;

/** Pull the date out of any row that uses TrxDate / trx_date / ds / date. */
export function pickDate(row: Record<string, unknown>): string {
  const raw = row.TrxDate ?? row.trx_date ?? row.ds ?? row.date ?? "";
  return String(raw).slice(0, 10);
}
