import {
  BarChart as RechartsBarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  LabelList,
  Cell,
} from "recharts";
import EmptyState from "@/components/ui/EmptyState";
import {
  AXIS_PROPS,
  CHART_COLOR,
  TOOLTIP_PROPS,
} from "./theme";

export interface HBarDatum {
  /** Y-axis label (route code, customer code, etc.). */
  label: string;
  /** Bar length. */
  value: number;
  /** Pre-formatted text shown at the end of the bar. */
  display?: string;
}

interface Props {
  data: HBarDatum[];
  /** Pixel height per bar; total chart height = N × this + axis padding. */
  rowHeight?: number;
  /** Single-series colour (defaults to brand primary). */
  color?: string;
  /** Width reserved for the y-axis labels in px. */
  labelWidth?: number;
  emptyMessage?: string;
}

/** Horizontal bar chart for top-N rankings; caller pre-sorts, bars show their formatted value. */
export default function HorizontalBarChart({
  data,
  rowHeight = 36,
  color = CHART_COLOR.brandPrimary,
  labelWidth = 96,
  emptyMessage = "No data",
}: Props) {
  if (!data.length) {
    return <EmptyState title={emptyMessage} />;
  }

  // Grows with N so bars never squash; +24 covers axis tick labels.
  const height = data.length * rowHeight + 24;

  return (
    <ResponsiveContainer width="100%" height={height}>
      <RechartsBarChart
        layout="vertical"
        data={data}
        margin={{ top: 6, right: 56, left: 6, bottom: 6 }}
        barCategoryGap={6}
      >
        <XAxis type="number" hide />
        <YAxis
          type="category"
          dataKey="label"
          width={labelWidth}
          {...AXIS_PROPS}
        />
        <Tooltip
          {...TOOLTIP_PROPS}
          formatter={(value: number, _name, entry) => {
            const display = (entry?.payload as HBarDatum | undefined)?.display;
            return [display ?? value.toLocaleString(), ""];
          }}
        />
        <Bar
          dataKey="value"
          radius={[4, 4, 4, 4]}
          isAnimationActive={false}
        >
          {data.map((d, i) => (
            <Cell key={d.label} fill={color} fillOpacity={0.55 + (0.45 * (data.length - i)) / data.length} />
          ))}
          <LabelList
            dataKey="display"
            position="right"
            offset={8}
            style={{
              fill: "var(--color-text-secondary, #475569)",
              fontSize: "0.8125rem",
              fontWeight: 600,
            }}
          />
        </Bar>
      </RechartsBarChart>
    </ResponsiveContainer>
  );
}
