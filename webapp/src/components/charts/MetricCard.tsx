import { useState, useEffect, useRef } from "react";
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

// Matches strings with exactly ONE numeric value (possibly with prefix/suffix
// like "AED 45.8K" or "55.3%"). Compound values like "1 / 21" or "12 routes"
// contain multiple number groups and must NOT be animated — stripping non-digits
// would concatenate them into a wrong number.
const SINGLE_NUMBER = /^[^0-9]*(\d+\.?\d*)[^0-9]*$/;

function useAnimatedValue(target: string): string {
  const [display, setDisplay] = useState(target);
  const prevRef = useRef(target);

  useEffect(() => {
    const prev = prevRef.current;
    prevRef.current = target;

    // Only animate single-number values to avoid corrupting "1 / 21" → "121"
    const prevMatch = prev.match(SINGLE_NUMBER);
    const targetMatch = target.match(SINGLE_NUMBER);
    const prevNum = prevMatch ? parseFloat(prevMatch[1]) : NaN;
    const targetNum = targetMatch ? parseFloat(targetMatch[1]) : NaN;
    if (isNaN(prevNum) || isNaN(targetNum) || prevNum === targetNum) {
      setDisplay(target);
      return;
    }

    const duration = 400; // ms
    const start = performance.now();
    let rafId: number;

    const step = (now: number) => {
      const progress = Math.min((now - start) / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3); // ease-out cubic
      const current = prevNum + (targetNum - prevNum) * eased;

      // Preserve the original format (currency, %, etc.)
      const formatted = target.replace(
        /[0-9]+\.?[0-9]*/,
        current.toFixed(target.includes(".") ? 1 : 0),
      );
      setDisplay(formatted);

      if (progress < 1) {
        rafId = requestAnimationFrame(step);
      }
    };
    rafId = requestAnimationFrame(step);

    return () => cancelAnimationFrame(rafId);
  }, [target]);

  return display;
}

export default function MetricCard({
  label,
  value,
  trend,
  subtitle,
  className = "",
  loading = false,
}: MetricCardProps) {
  const animatedValue = useAnimatedValue(String(value));

  return (
    <div
      className={[
        "bg-surface-sunken border-l-3 border-brand-200 rounded-lg p-4",
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
          <div className="flex items-baseline gap-2 animate-fadeIn">
            <span className="text-xl font-bold text-text-primary">{animatedValue}</span>
            {trend && (
              <span className={`text-body font-medium ${trendConfig[trend].color}`}>
                {trendConfig[trend].icon}
              </span>
            )}
          </div>
          {subtitle && <p className="text-caption text-text-tertiary mt-1">{subtitle}</p>}
        </>
      )}
    </div>
  );
}
