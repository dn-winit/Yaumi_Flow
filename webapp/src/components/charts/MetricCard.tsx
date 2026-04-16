import { Skeleton } from "@/components/ui/Skeleton";

type Trend = "up" | "down" | "neutral";

interface MetricCardProps {
  label: string;
  value: string | number;
  trend?: Trend;
  subtitle?: string;
  className?: string;
  loading?: boolean;
}

const trendConfig: Record<Trend, { icon: string; color: string }> = {
  up:      { icon: "\u2191", color: "text-success-600" },
  down:    { icon: "\u2193", color: "text-danger-600" },
  neutral: { icon: "\u2192", color: "text-text-tertiary" },
};

export default function MetricCard({
  label,
  value,
  trend,
  subtitle,
  className = "",
  loading = false,
}: MetricCardProps) {
  return (
    <div
      className={[
        "bg-surface-raised rounded-xl shadow-1 border border-default p-6",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <p className="text-caption font-medium text-text-tertiary uppercase tracking-wider mb-1">
        {label}
      </p>
      {loading ? (
        <>
          <Skeleton className="h-7 w-28 mb-2" />
          {subtitle && <Skeleton className="h-3 w-32" />}
        </>
      ) : (
        <>
          <div className="flex items-baseline gap-2">
            <span className="text-2xl font-bold text-text-primary">{value}</span>
            {trend && (
              <span className={`text-sm font-medium ${trendConfig[trend].color}`}>
                {trendConfig[trend].icon}
              </span>
            )}
          </div>
          {subtitle && <p className="text-sm text-text-tertiary mt-1">{subtitle}</p>}
        </>
      )}
    </div>
  );
}
