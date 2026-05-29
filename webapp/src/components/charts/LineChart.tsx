import type { ReactNode } from "react";
import {
  LineChart as RechartsLineChart,
  Line,
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

// Hide dots above ~14 points (≈ half a month) so dense lines stay clean; sparse ones keep markers.
const _DOT_DENSITY_HIDE_AT = 14;
const _DOT_RADIUS = 3;
const _ACTIVE_DOT_RADIUS = 5;

interface LineSeries {
  key: string;
  label?: string;
  /** Optional override; defaults to CHART_PALETTE[index]. */
  color?: string;
}

interface LineChartProps {
  data: Record<string, unknown>[];
  xKey: string;
  series: LineSeries[];
  height?: number;
  title?: string;
  /** Optional helper text rendered just below the title, inside the card. */
  subtitle?: string;
  /** Right-side header slot for toggles/chips; matches Card.actions. */
  actions?: ReactNode;
  emptyMessage?: string;
  loading?: boolean;
  /** Minimum px per data point; wrapper scrolls when overflow. */
  pxPerPoint?: number;
}

export default function LineChart({
  data,
  xKey,
  series,
  height = DEFAULT_CHART_HEIGHT,
  title,
  subtitle,
  actions,
  emptyMessage = "No data",
  loading = false,
  pxPerPoint = 80,
}: LineChartProps) {
  // Title/subtitle on the left, actions on the right; mirrors Card + BarChart.
  const header =
    title || subtitle || actions ? (
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          {title && <h3 className="text-title font-semibold text-text-primary">{title}</h3>}
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

  return (
    <div className="bg-surface-raised rounded-xl shadow-1 border border-default p-6">
      {header}
      {data.length === 0 ? (
        <EmptyState title={emptyMessage} />
      ) : (
        <div className="overflow-x-auto">
          <div style={{ minWidth: `${data.length * pxPerPoint}px` }}>
            <ResponsiveContainer width="100%" height={height}>
              <RechartsLineChart data={data} margin={{ top: 5, right: 20, left: 0, bottom: 12 }}>
                <CartesianGrid {...GRID_PROPS} />
                {/* interval=0 + scroll wrapper -> every label gets its own column. */}
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
                {series.length > 1 && <Legend wrapperStyle={{ fontSize: "0.875rem" }} />}
                {series.map((s, idx) => {
                  const stroke = s.color ?? CHART_PALETTE[idx % CHART_PALETTE.length];
                  // Adaptive dot: count this series' real points; hide when dense.
                  const populated = data.reduce((n, row) => n + (row[s.key] != null ? 1 : 0), 0);
                  const showDots = populated <= _DOT_DENSITY_HIDE_AT;
                  return (
                    <Line
                      key={s.key}
                      type="monotone"
                      dataKey={s.key}
                      name={s.label ?? s.key}
                      stroke={stroke}
                      strokeWidth={2}
                      dot={showDots ? { r: _DOT_RADIUS, fill: stroke } : false}
                      activeDot={{ r: _ACTIVE_DOT_RADIUS }}
                      connectNulls={false}
                    />
                  );
                })}
              </RechartsLineChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}
    </div>
  );
}
