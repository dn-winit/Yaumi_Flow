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
import {
  AXIS_PROPS,
  CHART_PALETTE,
  DEFAULT_CHART_HEIGHT,
  GRID_PROPS,
  TOOLTIP_PROPS,
} from "./theme";

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
  emptyMessage?: string;
}

export default function LineChart({
  data,
  xKey,
  series,
  height = DEFAULT_CHART_HEIGHT,
  title,
  emptyMessage = "No data",
}: LineChartProps) {
  return (
    <div className="bg-surface-raised rounded-xl shadow-1 border border-default p-6">
      {title && (
        <h3 className="text-title font-semibold text-text-primary mb-4">{title}</h3>
      )}
      {data.length === 0 ? (
        <EmptyState title={emptyMessage} />
      ) : (
        <ResponsiveContainer width="100%" height={height}>
          <RechartsLineChart
            data={data}
            margin={{ top: 5, right: 20, left: 0, bottom: 5 }}
          >
            <CartesianGrid {...GRID_PROPS} />
            <XAxis dataKey={xKey} {...AXIS_PROPS} />
            <YAxis {...AXIS_PROPS} />
            <Tooltip {...TOOLTIP_PROPS} />
            {series.length > 1 && <Legend wrapperStyle={{ fontSize: "0.875rem" }} />}
            {series.map((s, idx) => {
              const stroke = s.color ?? CHART_PALETTE[idx % CHART_PALETTE.length];
              return (
                <Line
                  key={s.key}
                  type="monotone"
                  dataKey={s.key}
                  name={s.label ?? s.key}
                  stroke={stroke}
                  strokeWidth={2}
                  dot={{ r: 3, fill: stroke }}
                  activeDot={{ r: 5 }}
                />
              );
            })}
          </RechartsLineChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
