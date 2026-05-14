import type { ReactNode } from "react";

/** Single option for a ToggleGroup. ``value`` is the discriminator the
 *  parent compares against; ``label`` is what renders. */
export interface ToggleOption<T extends string> {
  value: T;
  label: ReactNode;
}

interface Props<T extends string> {
  options: ToggleOption<T>[];
  value: T;
  onChange: (v: T) => void;
  /** Optional accessible name for assistive tech (e.g. "Chart view"). */
  ariaLabel?: string;
}

/**
 * Pill-style segmented toggle used in chart card headers. Generic over
 * the option type so each call site keeps its own union (e.g.
 * ``"revenue" | "quantity"`` vs ``"van_load" | "leftovers"``). Style is
 * the single source of truth across the app -- DashboardDailyTrend,
 * AccuracyDrawer, and anything that adopts a header toggle render the
 * exact same look + behaviour.
 */
export default function ToggleGroup<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
}: Props<T>) {
  return (
    <div
      role="group"
      aria-label={ariaLabel}
      className="inline-flex items-center rounded-md border border-default bg-surface-base p-0.5 gap-0.5"
    >
      {options.map((opt) => {
        const isActive = opt.value === value;
        return (
          <button
            key={opt.value}
            type="button"
            onClick={() => onChange(opt.value)}
            aria-pressed={isActive}
            className={[
              "px-2.5 py-1 text-caption font-semibold rounded transition-all duration-base whitespace-nowrap",
              isActive
                ? "bg-brand-600 text-white"
                : "text-text-secondary hover:text-text-primary hover:bg-surface-sunken",
            ].join(" ")}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
