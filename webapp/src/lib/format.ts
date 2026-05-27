/** Shared number / currency / date formatting helpers. */

export function toNum(v: unknown): number | null {
  if (v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

const MISSING = "--";

/** Locale-formatted number, no abbreviation -- raw counts so tiles are verifiable. */
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

/** Format a 0..100-scale percentage (e.g. 87.4 -> "87.4%"); MISSING for null/NaN. */
export function fmtPct(v: unknown, digits = 1): string {
  const n = toNum(v);
  if (n == null) return MISSING;
  return `${n.toFixed(digits)}%`;
}

/** Format a 0..1 fraction as a whole-number percent: 0.20 -> "20%". */
export function fmtBps(v: unknown): string {
  const n = toNum(v);
  if (n == null) return MISSING;
  return `${Math.round(n * 100)}%`;
}

/** Format a [lo, hi] range; MISSING when either bound is unparseable. */
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

/** Shared score thresholds -- single source so KPI tiles, badges, and risk panels agree.
 *  PROBABILISTIC_DEMAND_CLASSES: classes that emit a real p_demand (vs synthetic 0/1). */
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

/** Van-load accuracy thresholds (Past-analysis drawer). TOLERANCE_PCT 20% mirrors the
 *  supervision perfect-zone band and recommended_order adoption tolerance (manually aligned). */
export const TOLERANCE_PCT = 0.2;
export const LEAKAGE_SHARE_WARN = 0.05;

// Per-class miss tolerance (presentation only); mirror of
// demand_forecasting_pipeline/src/evaluation/metrics.py.
export const TOLERANCE_BY_CLASS: Readonly<Record<string, number>> = {
  smooth:       0.10,
  intermittent: 0.20,
  erratic:      0.30,
  lumpy:        0.40,
};

/** Recommendation-adoption thresholds: delivery-good cutoff + on-target arrow band. */
export const DELIVERY_GOOD = 80;
export const ON_TARGET_GOOD_RATIO = 0.7;
export const ON_TARGET_POOR_RATIO = 0.4;

/** Top-N rows on dashboard ranking charts (presentation cap; endpoint still returns 10). */
export const DASHBOARD_TOP_N = 5;

/** Per-route per-day recommendation cap for the Visit step (covers ~150 cust × ~20 SKUs). */
export const VISIT_REC_LIMIT = 5000;

/** UI timing constants (ms). OVERLAY_EXIT_MS must match Modal/Drawer transition classes. */
export const OVERLAY_EXIT_MS = 200;
export const TOAST_AUTO_DISMISS_MS = 3000;

/** Pull the date out of any row that uses TrxDate / trx_date / ds / date. */
export function pickDate(row: Record<string, unknown>): string {
  const raw = row.TrxDate ?? row.trx_date ?? row.ds ?? row.date ?? "";
  return String(raw).slice(0, 10);
}
