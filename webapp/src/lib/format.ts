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

/**
 * Shared score thresholds. All consumers (KPI tiles, badges, risk panels,
 * confidence chips) read from these so a change here flips every surface
 * in lockstep.
 *  - GOOD_SCORE_THRESHOLD: % cut-off for "good" score colouring.
 *  - AT_RISK_CONFIDENCE:    p_demand floor below which an item is "risky".
 *  - STRONG_CONFIDENCE:     p_demand floor for the green confidence chip.
 *
 * The two confidence values are also the danger / warning breakpoints
 * the ConfidenceBadge uses, so badge colour and "Risky items" tile
 * cannot disagree about what counts as low confidence.
 *
 * Demand classes that produce a real probability (two-stage models).
 * Smooth and erratic models emit a binary 0/1 fallback that should not
 * be rendered as a real percentage -- the badge hides itself for them.
 */
export const GOOD_SCORE_THRESHOLD = 75;
export const AT_RISK_CONFIDENCE = 0.7;
export const STRONG_CONFIDENCE = 0.9;
export const PROBABILISTIC_DEMAND_CLASSES: ReadonlySet<string> = new Set([
  "intermittent",
  "lumpy",
]);

/** True when the row's class produces a real p_demand (vs synthetic 0/1). */
export function hasRealConfidence(demandClass: string | null | undefined): boolean {
  if (demandClass == null) return false;
  return PROBABILISTIC_DEMAND_CLASSES.has(String(demandClass).trim().toLowerCase());
}

/**
 * Van-load accuracy thresholds used on the Past-analysis drawer.
 *  - TOLERANCE_PCT: a day is "on target" when |predicted - actual| / actual is
 *    within this fraction. 20% mirrors the supervision perfect-zone ±20% band
 *    AND the recommended_order adoption tolerance (kept aligned manually since
 *    Python and TS can't share a single source).
 *  - LEAKAGE_SHARE_WARN: an item's window volume must exceed this share of
 *    route totals before it's eligible for the "most accurate item" highlight
 *    (keeps a 1-unit SKU from hijacking the strip).
 */
export const TOLERANCE_PCT = 0.2;
export const LEAKAGE_SHARE_WARN = 0.05;

// Per-class miss tolerance for composite accuracy. Mirrors
// TOLERANCE_BY_CLASS in demand_forecasting_pipeline/src/evaluation/metrics.py
// (kept in lockstep manually).
export const TOLERANCE_BY_CLASS: Readonly<Record<string, number>> = {
  smooth:       0.10,
  intermittent: 0.20,
  erratic:      0.30,
  lumpy:        0.40,
};
export const DEFAULT_CLASS_TOLERANCE = 0.20;

/** Composite accuracy on the client -- mirror of Python composite_summary. */
export function compositeAccuracy(
  rows: { predicted: number; actual: number; demandClass?: string | null }[],
): number | null {
  let scoredActual = 0;
  let realMiss = 0;
  for (const r of rows) {
    if (!(r.predicted > 0) || !(r.actual > 0)) continue;
    const tol =
      TOLERANCE_BY_CLASS[String(r.demandClass ?? "").trim().toLowerCase()] ??
      DEFAULT_CLASS_TOLERANCE;
    const absErr = Math.abs(r.predicted - r.actual);
    const allowed = tol * r.actual;
    realMiss += Math.max(0, absErr - allowed);
    scoredActual += r.actual;
  }
  if (scoredActual <= 0) return null;
  return Math.max(0, 100 - (realMiss / scoredActual) * 100);
}

/**
 * Recommendation-adoption thresholds.
 *  - DELIVERY_GOOD: green when volume/revenue delivered >= this % of recommended.
 *  - ON_TARGET_GOOD_RATIO / ON_TARGET_POOR_RATIO: arrow direction on the
 *    "On-target days" tile -- up when at least this share of scored days
 *    landed within tolerance, down when below the poor cutoff.
 */
export const DELIVERY_GOOD = 80;
export const ON_TARGET_GOOD_RATIO = 0.7;
export const ON_TARGET_POOR_RATIO = 0.4;

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
  { key: "last_30_working_days", label: "Last 30 working days", days: 30 },
] as const;
export type Lookback = (typeof LOOKBACK_OPTIONS)[number]["key"];
export const DEFAULT_LOOKBACK: Lookback = "last_30_working_days";

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

/**
 * Per-route per-day recommendation cap pulled into the Visit step. A real
 * route holds ~50-150 customers x ~20 SKUs each, so a few thousand rows
 * covers the largest routes with headroom and lets us render in one pass
 * without paging the live session.
 */
export const VISIT_REC_LIMIT = 5000;

/** Pull the date out of any row that uses TrxDate / trx_date / ds / date. */
export function pickDate(row: Record<string, unknown>): string {
  const raw = row.TrxDate ?? row.trx_date ?? row.ds ?? row.date ?? "";
  return String(raw).slice(0, 10);
}
