import type { ReactNode } from "react";

/** ToggleGroup option; value = discriminator, label = render. */
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

/** Pill-style segmented toggle; generic over the option type. */
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
