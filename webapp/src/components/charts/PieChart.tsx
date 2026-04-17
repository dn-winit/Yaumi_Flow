import {
  PieChart as RechartsPieChart,
  Pie,
  Cell,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from "recharts";
import EmptyState from "@/components/ui/EmptyState";
import {
  CHART_PALETTE,
  DEFAULT_CHART_HEIGHT,
  TOOLTIP_PROPS,
} from "./theme";

interface PieChartProps {
  data: Record<string, unknown>[];
  dataKey: string;
  nameKey: string;
  /** Optional palette override; defaults to CHART_PALETTE. */
  colors?: readonly string[];
  height?: number;
  title?: string;
  emptyMessage?: string;
  loading?: boolean;
}

export default function PieChart({
  data,
  dataKey,
  nameKey,
  colors = CHART_PALETTE,
  height = DEFAULT_CHART_HEIGHT,
  title,
  emptyMessage = "No data",
  loading = false,
}: PieChartProps) {
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
          <RechartsPieChart>
            <Pie
              data={data}
              cx="50%"
              cy="50%"
              innerRadius={60}
              outerRadius={90}
              paddingAngle={2}
              dataKey={dataKey}
              nameKey={nameKey}
            >
              {data.map((_entry, index) => (
                <Cell key={index} fill={colors[index % colors.length]} />
              ))}
            </Pie>
            <Tooltip {...TOOLTIP_PROPS} />
            <Legend
              iconType="circle"
              iconSize={8}
              wrapperStyle={{ fontSize: "0.875rem" }}
            />
          </RechartsPieChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
