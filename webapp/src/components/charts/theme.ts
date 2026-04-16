/**
 * Shared Recharts theme. Every chart primitive spreads these props directly
 * so series colouring, axes, gridlines, and tooltips stay visually identical
 * across the app. Derived from `tokens.chart` — do not introduce chart-local
 * overrides, extend the token instead.
 */
import { tokens } from "@/theme/tokens";

const { chart, color } = tokens;

/**
 * Series palette, indexable by position. Index 0 is the Yaumi crimson
 * (brand-600), index 1 is the Yaumi gold (accent-500). All values are
 * pulled from the scales in `tokens.ts` — no parallel hex lives here.
 */
export const CHART_PALETTE: readonly string[] = [
  color.brand[600],
  color.accent[500],
  color.info[500],
  color.success[500],
  color.warning[500],
  color.danger[500],
  color.neutral[500],
  color.brand[300],
] as const;

/**
 * Semantic chart colours — use these for single-series bar/line charts that
 * need a tone (e.g. success/danger/warning) rather than a palette index.
 * Kept in sync with `tokens.ts` so every chart colour comes from one source.
 */
export const CHART_COLOR = {
  brandPrimary: color.brand[600],
  brandBand:    color.brand[100],
  success:      color.success[600],
  danger:       color.danger[500],
  warning:      color.warning[500],
  info:         color.info[500],
} as const;

/** Default Recharts chart height in px. */
export const DEFAULT_CHART_HEIGHT = 280;

/** Spread onto <CartesianGrid />. */
export const GRID_PROPS = {
  stroke: chart.grid.stroke,
  strokeDasharray: "3 3",
} as const;

/** Spread onto <XAxis /> and <YAxis />. */
export const AXIS_PROPS = {
  stroke: chart.axis.stroke,
  tick: { fill: chart.axis.fill, fontSize: chart.axis.fontSize },
  tickLine: false,
  axisLine: { stroke: chart.axis.stroke },
} as const;

/** Spread onto <Tooltip />. */
export const TOOLTIP_PROPS = {
  contentStyle: {
    background: chart.tooltip.background,
    border: `1px solid ${chart.tooltip.border}`,
    borderRadius: chart.tooltip.radius,
    boxShadow: chart.tooltip.shadow,
    color: chart.tooltip.color,
    fontSize: "0.875rem",
  },
  cursor: { fill: chart.grid.stroke, fillOpacity: 0.4 },
} as const;
