import { useState, useEffect, useRef, type ReactNode } from "react";
import { Skeleton } from "@/components/ui/Skeleton";

type Trend = "up" | "down" | "neutral";

interface MetricCardProps {
  label: string;
  /** Primitives animate (count-up); ReactNodes render verbatim. */
  value: string | number | ReactNode;
  trend?: Trend;
  subtitle?: string | ReactNode;
  className?: string;
  loading?: boolean;
  info?: ReactNode;
  disableAnimation?: boolean;
}

const trendConfig: Record<Trend, { icon: string; color: string }> = {
  up: { icon: "\u2191", color: "text-success-600" },
  down: { icon: "\u2193", color: "text-danger-600" },
  neutral: { icon: "\u2192", color: "text-text-tertiary" },
};

// Single-number guard so "1 / 21" doesn't animate into "121".
const SINGLE_NUMBER = /^[^0-9]*(\d+\.?\d*)[^0-9]*$/;

function useAnimatedValue(target: string): string {
  const [display, setDisplay] = useState(target);
  const prevRef = useRef(target);

  useEffect(() => {
    const prev = prevRef.current;
    prevRef.current = target;

    // Only animate single-number values to avoid corrupting "1 / 21" -> "121"
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

// Animate only primitives; ReactNodes would coerce to "[object Object]" or flicker.
function isAnimatable(v: unknown): v is string | number {
  return typeof v === "string" || typeof v === "number";
}

export default function MetricCard({
  label,
  value,
  trend,
  subtitle,
  className = "",
  loading = false,
  info,
  disableAnimation = false,
}: MetricCardProps) {
  const isPrimitive = isAnimatable(value);
  const primitive = isPrimitive ? String(value) : "";
  const animatedValue = useAnimatedValue(primitive);
  const rendered: ReactNode = !isPrimitive ? value : disableAnimation ? primitive : animatedValue;

  return (
    <div
      className={["bg-surface-sunken border-l-3 border-brand-200 rounded-lg p-4", className]
        .filter(Boolean)
        .join(" ")}
    >
      <p className="text-caption font-medium text-text-tertiary uppercase tracking-wider mb-1 flex items-center gap-1.5">
        <span>{label}</span>
        {info}
      </p>
      {loading ? (
        <>
          <Skeleton className="h-7 w-28 mb-2" />
          {subtitle && <Skeleton className="h-3 w-32" />}
        </>
      ) : (
        <>
          <div className="flex flex-wrap items-baseline gap-2 animate-fadeIn">
            <span className="text-xl font-bold text-text-primary">{rendered}</span>
            {trend && (
              <span className={`text-body font-medium ${trendConfig[trend].color}`}>
                {trendConfig[trend].icon}
              </span>
            )}
          </div>
          {subtitle && <div className="text-caption text-text-tertiary mt-1">{subtitle}</div>}
        </>
      )}
    </div>
  );
}
