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

/**
 * Format a number in full, with locale thousands separators (e.g. "6,576").
 *
 * The previous implementation abbreviated to ``K`` / ``M`` once the value
 * crossed 1,000 / 1,000,000. That hurt cross-tile comparisons -- two views
 * showing "6.2K" and "6.6K" look like the same rounded number even when
 * the underlying difference is 400 units. Operators asked for raw counts
 * everywhere so each tile is directly verifiable against the data, and
 * rounding only happens where an average is being shown (caller passes
 * ``digits`` explicitly in those places).
 */
export function fmtNum(v: unknown, digits = 0): string {
  const n = toNum(v);
  if (n == null) return MISSING;
  return n.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function fmtCurrency(v: unknown, digits = 2): string {
  const n = toNum(v);
  if (n == null) return MISSING;
  return `AED ${n.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

/**
 * Format a percentage already on a 0..100 scale (e.g. ``87.4``).
 * Returns ``MISSING`` for null / NaN. Default 1 decimal to match the
 * tile / chart rendering convention used across the app.
 */
export function fmtPct(v: unknown, digits = 1): string {
  const n = toNum(v);
  if (n == null) return MISSING;
  return `${n.toFixed(digits)}%`;
}

/**
 * Format a basis-point fraction (0..1) as a whole-number percentage.
 * Used for tolerance / threshold display: ``fmtBps(0.20)`` -> ``"20%"``.
 */
export function fmtBps(v: unknown): string {
  const n = toNum(v);
  if (n == null) return MISSING;
  return `${Math.round(n * 100)}%`;
}

/**
 * Format a [lo, hi] confidence interval / range with a single decimal
 * by default. Returns ``MISSING`` when either bound is unparseable so
 * the UI never renders ``NaN - NaN``.
 */
export function fmtRange(lo: unknown, hi: unknown, digits = 1): string {
  const a = toNum(lo);
  const b = toNum(hi);
  if (a == null || b == null) return MISSING;
  return `${a.toFixed(digits)} – ${b.toFixed(digits)}`;
}

/** Format a duration in seconds as ``Xs`` / ``Xm`` / ``Xm Ys``. */
export function fmtDuration(seconds: unknown): string {
  const n = toNum(seconds);
  if (n == null || n <= 0) return "0s";
  if (n < 60) return `${Math.round(n)}s`;
  const m = Math.floor(n / 60);
  const s = Math.round(n % 60);
  return s > 0 ? `${m}m ${s}s` : `${m}m`;
}

/** Format a file size in megabytes as ``X.X MB`` / ``X.X GB``. */
export function fmtFileSize(mb: unknown): string {
  const n = toNum(mb);
  if (n == null) return MISSING;
  if (n >= 1024) return `${(n / 1024).toFixed(2)} GB`;
  return `${n.toFixed(1)} MB`;
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

// Per-class miss tolerance for composite accuracy. Read by the
// ForecastAccuracyExplanation popup so the displayed tolerance band
// stays in lockstep with the server-side definition in
// demand_forecasting_pipeline/src/evaluation/metrics.py. The full
// composite-accuracy calculation itself runs server-side; this table
// is presentation-only (label copy).
export const TOLERANCE_BY_CLASS: Readonly<Record<string, number>> = {
  smooth:       0.10,
  intermittent: 0.20,
  erratic:      0.30,
  lumpy:        0.40,
};

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

/**
 * UI timing constants (milliseconds). One declaration so transitions
 * line up across modals, drawers, and toast dismiss timers without each
 * caller picking its own number.
 *
 * - ``OVERLAY_EXIT_MS`` matches the ``transition-opacity`` / ``transform``
 *   classes on Modal + Drawer (~200ms). Used by the unmount setTimeout
 *   so the exit animation completes before React removes the node.
 * - ``TOAST_AUTO_DISMISS_MS`` is the default lifetime of a toast.
 */
export const OVERLAY_EXIT_MS = 200;
export const TOAST_AUTO_DISMISS_MS = 3000;

/** Pull the date out of any row that uses TrxDate / trx_date / ds / date. */
export function pickDate(row: Record<string, unknown>): string {
  const raw = row.TrxDate ?? row.trx_date ?? row.ds ?? row.date ?? "";
  return String(raw).slice(0, 10);
}
