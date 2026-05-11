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

// Density thresholds for adaptive dot rendering. Above the upper bound the
// line stands alone (a dot at every working day in a month makes the trend
// noisy); below it dots stay because each point carries individual weight.
// Tuned to ~14 working days = ½ month so a "two weeks" view shows markers
// while a "30 days" view stays clean.
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
  emptyMessage?: string;
  loading?: boolean;
  /**
   * Minimum horizontal width allocated per data point. When the chart
   * width exceeds the container, the wrapper scrolls horizontally so
   * every tick stays legible. Default 80px is wide enough for a
   * `dd-mm-yyyy` label at the standard axis font size without crowding
   * adjacent labels.
   */
  pxPerPoint?: number;
}

export default function LineChart({
  data,
  xKey,
  series,
  height = DEFAULT_CHART_HEIGHT,
  title,
  subtitle,
  emptyMessage = "No data",
  loading = false,
  pxPerPoint = 80,
}: LineChartProps) {
  // Title/subtitle pair sits in one stacked block so the spacing rule is
  // simple: the *bottom* element (subtitle when present, otherwise title)
  // owns the gap to the chart. Avoids a dangling mb-4 when both are blank.
  const header = (title || subtitle) ? (
    <div className="mb-4">
      {title && (
        <h3 className="text-title font-semibold text-text-primary">{title}</h3>
      )}
      {subtitle && (
        <p className={`text-caption text-text-tertiary ${title ? "mt-1" : ""}`}>{subtitle}</p>
      )}
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
              <RechartsLineChart
                data={data}
                margin={{ top: 5, right: 20, left: 0, bottom: 12 }}
              >
                <CartesianGrid {...GRID_PROPS} />
                {/* interval=0 + minTickGap=0 forces every working day
                    to render. The wrapper's overflow-x-auto + minWidth
                    guarantees each label has its own column, so labels
                    never collide. */}
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
                  // Adaptive dot: count this series' actual datapoints (skip
                  // nulls/undefined that pad gaps in sparse windows). When
                  // the line is dense, dots add noise; when sparse, they
                  // anchor the eye on the few real datapoints. Active-dot
                  // (hover) always renders so tooltips stay discoverable.
                  const populated = data.reduce(
                    (n, row) => n + (row[s.key] != null ? 1 : 0),
                    0,
                  );
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
