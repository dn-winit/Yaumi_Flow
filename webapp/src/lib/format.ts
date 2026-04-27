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
 *  - LEAKAGE_SHARE_WARN: an item's 30-day volume must exceed this share of
 *    route totals before it's eligible for the "most accurate item" highlight
 *    (keeps a 1-unit SKU from hijacking the strip).
 */
export const TOLERANCE_PCT = 0.2;
export const LEAKAGE_SHARE_WARN = 0.05;

/**
 * Recommendation-adoption thresholds (used in the Last-30-Days drawers).
 *  - DELIVERY_GOOD: green when volume/revenue delivered >= this % of recommended.
 *  - TREND_STEP_PP: percentage-point change that counts as improving/declining;
 *    smaller deltas are shown as "stable".
 *  - ON_TARGET_GOOD_RATIO / ON_TARGET_POOR_RATIO: arrow direction on the
 *    "On-target days" tile -- up when at least this share of scored days
 *    landed within tolerance, down when below the poor cutoff.
 */
export const DELIVERY_GOOD = 80;
export const TREND_STEP_PP = 2;
export const ON_TARGET_GOOD_RATIO = 0.7;
export const ON_TARGET_POOR_RATIO = 0.4;

/**
 * Shared Last-30-Days window size. Both the Adoption drawer and VanLoad
 * accuracy drawer query the same span, so they import the same constant.
 * Changing here updates both dashboards + their context-bar labels.
 */
export const LAST_30_DAYS = 30;

/**
 * Reporting-period options for the Dashboard page. A "working day" is any
 * date with actual sales activity in sales_recent.csv, so weekends,
 * public holidays, and any other closure are excluded automatically.
 * Drives sales + customer overview hooks together so every chart and
 * table rolls on the same period. Keys + day counts mirror the backend
 * enum (data_import.services.eda_service.LOOKBACK_OPTIONS) -- single
 * source of truth, no scattered magic numbers.
 */
export const LOOKBACK_OPTIONS = [
  { key: "last_working_day", label: "Last working day", days: 1 },
  { key: "last_7_working_days", label: "Last 7 working days", days: 7 },
] as const;
export type Lookback = (typeof LOOKBACK_OPTIONS)[number]["key"];
export const DEFAULT_LOOKBACK: Lookback = "last_7_working_days";

export function lookbackLabel(key: Lookback): string {
  return LOOKBACK_OPTIONS.find((o) => o.key === key)?.label ?? key;
}

export function lookbackDays(key: Lookback): number {
  return LOOKBACK_OPTIONS.find((o) => o.key === key)?.days ?? 7;
}

/**
 * Top-N rows shown in dashboard ranking charts. Five is enough to convey the
 * leader board without making the page scroll-heavy; the underlying endpoint
 * still returns top 10 so this is a presentation cap, not a data limit.
 */
export const DASHBOARD_TOP_N = 5;

/** Pull the date out of any row that uses TrxDate / trx_date / ds / date. */
export function pickDate(row: Record<string, unknown>): string {
  const raw = row.TrxDate ?? row.trx_date ?? row.ds ?? row.date ?? "";
  return String(raw).slice(0, 10);
}
