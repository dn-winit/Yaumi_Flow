import {
  BarChart as RechartsBarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
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

interface BarChartProps {
  data: Record<string, unknown>[];
  xKey: string;
  yKey: string;
  /** Optional override; defaults to CHART_PALETTE[0] (brand primary). */
  color?: string;
  height?: number;
  title?: string;
  emptyMessage?: string;
  onBarClick?: (payload: Record<string, unknown>) => void;
  loading?: boolean;
}

export default function BarChart({
  data,
  xKey,
  yKey,
  color = CHART_PALETTE[0],
  height = DEFAULT_CHART_HEIGHT,
  title,
  emptyMessage = "No data",
  onBarClick,
  loading = false,
}: BarChartProps) {
  if (loading) {
    return (
      <div className="bg-surface-raised rounded-xl shadow-1 border border-default p-6">
        {title && (
          <h3 className="text-title font-semibold text-text-primary mb-4">{title}</h3>
        )}
        <div className="animate-pulse bg-surface-sunken rounded-lg" style={{ height }} />
      </div>
    );
  }

  return (
    <div className="bg-surface-raised rounded-xl shadow-1 border border-default p-6">
      {title && (
        <h3 className="text-title font-semibold text-text-primary mb-4">{title}</h3>
      )}
      {data.length === 0 ? (
        <EmptyState title={emptyMessage} />
      ) : (
        <ResponsiveContainer width="100%" height={height}>
          <RechartsBarChart
            data={data}
            margin={{ top: 5, right: 20, left: 0, bottom: 5 }}
          >
            <CartesianGrid {...GRID_PROPS} />
            <XAxis dataKey={xKey} {...AXIS_PROPS} />
            <YAxis {...AXIS_PROPS} />
            <Tooltip {...TOOLTIP_PROPS} />
            <Bar
              dataKey={yKey}
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
          </RechartsBarChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
