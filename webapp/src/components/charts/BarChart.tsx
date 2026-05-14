import type { ReactNode } from "react";
import {
  BarChart as RechartsBarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";
import EmptyState from "@/components/ui/EmptyState";
import { fmtAxisDate } from "./formatters";
import {
  AXIS_PROPS,
  CHART_PALETTE,
  DEFAULT_CHART_HEIGHT,
  GRID_PROPS,
  TOOLTIP_PROPS,
} from "./theme";

/** Single bar definition for multi-series (grouped) mode. */
interface BarSeries {
  key: string;
  label?: string;
  /** Optional override; defaults to CHART_PALETTE[index]. */
  color?: string;
}

interface BarChartProps {
  data: Record<string, unknown>[];
  xKey: string;
  /** Single-series mode: one bar per x value reading ``yKey``. */
  yKey?: string;
  /** Multi-series mode: grouped bars per x value, one Bar per entry. */
  series?: BarSeries[];
  /** Single-series colour override. Ignored when ``series`` is provided. */
  color?: string;
  height?: number;
  title?: string;
  /** Optional helper text rendered just below the title, inside the card. */
  subtitle?: string;
  /** Optional element rendered on the right side of the header (intended
   *  for toggles, filter chips, etc.). Matches the Card ``actions`` slot. */
  actions?: ReactNode;
  emptyMessage?: string;
  onBarClick?: (payload: Record<string, unknown>) => void;
  loading?: boolean;
  /**
   * Minimum horizontal width allocated per bar (single-series) or per
   * GROUP (multi-series). The wrapper's ``overflow-x-auto`` ensures
   * crowded windows stay legible by scrolling instead of overlapping.
   */
  pxPerPoint?: number;
}

export default function BarChart({
  data,
  xKey,
  yKey,
  series,
  color = CHART_PALETTE[0],
  height = DEFAULT_CHART_HEIGHT,
  title,
  subtitle,
  actions,
  emptyMessage = "No data",
  onBarClick,
  loading = false,
  pxPerPoint = 80,
}: BarChartProps) {
  // Title/subtitle pair mirrors LineChart's stacked header so the two
  // components are visually interchangeable from the caller's POV.
  // ``actions`` sits on the right, matching the Card component's slot.
  const header = (title || subtitle || actions) ? (
    <div className="mb-4 flex items-start justify-between gap-3">
      <div>
        {title && (
          <h3 className="text-title font-semibold text-text-primary">{title}</h3>
        )}
        {subtitle && (
          <p className={`text-caption text-text-tertiary ${title ? "mt-1" : ""}`}>{subtitle}</p>
        )}
      </div>
      {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
    </div>
  ) : null;

  if (loading) {
    return (
      <div className="bg-surface-raised rounded-xl shadow-1 border border-default p-6">
        {header}
        <div className="animate-pulse bg-surface-sunken rounded-lg" style={{ height }} />
      </div>
    );
  }

  // Multi-series mode reserves wider space per x value so the grouped
  // bars don't collapse onto each other. ~32px per bar inside a group
  // is the smallest legible width at the standard axis font size.
  const isMulti = Array.isArray(series) && series.length > 0;
  const effectivePxPerPoint = isMulti
    ? Math.max(pxPerPoint, series!.length * 36)
    : pxPerPoint;

  return (
    <div className="bg-surface-raised rounded-xl shadow-1 border border-default p-6">
      {header}
      {data.length === 0 ? (
        <EmptyState title={emptyMessage} />
      ) : (
        <div className="overflow-x-auto">
          <div style={{ minWidth: `${data.length * effectivePxPerPoint}px` }}>
            <ResponsiveContainer width="100%" height={height}>
              <RechartsBarChart
                data={data}
                margin={{ top: 5, right: 20, left: 0, bottom: 12 }}
                barCategoryGap="20%"
              >
                <CartesianGrid {...GRID_PROPS} />
                {/* interval=0 + minTickGap=0 keeps every label visible;
                    the wrapper's overflow-x-auto + minWidth guarantees
                    each label has its own column with no collision. */}
                <XAxis
                  dataKey={xKey}
                  tickFormatter={fmtAxisDate}
                  interval={0}
                  minTickGap={0}
                  tickMargin={8}
                  {...AXIS_PROPS}
                />
                <YAxis {...AXIS_PROPS} />
                <Tooltip {...TOOLTIP_PROPS} labelFormatter={fmtAxisDate} />
                {isMulti && <Legend wrapperStyle={{ fontSize: "0.875rem" }} />}
                {isMulti ? (
                  series!.map((s, idx) => (
                    <Bar
                      key={s.key}
                      dataKey={s.key}
                      name={s.label ?? s.key}
                      fill={s.color ?? CHART_PALETTE[idx % CHART_PALETTE.length]}
                      radius={[4, 4, 0, 0]}
                    />
                  ))
                ) : (
                  <Bar
                    dataKey={yKey ?? ""}
                    fill={color}
                    radius={[4, 4, 0, 0]}
                    onClick={
                      onBarClick
                        ? (d: { payload?: Record<string, unknown> }) =>
                            d.payload && onBarClick(d.payload)
                        : undefined
                    }
                    style={onBarClick ? { cursor: "pointer" } : undefined}
                  />
                )}
              </RechartsBarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}
    </div>
  );
}
