import type { ReactNode } from "react";

/** Single highlight inside HighlightsStrip; detail dims below the headline value. */
export interface Highlight {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
}

interface HighlightsStripProps {
  items: Highlight[];
}

/** Positive-framed strip above drill-down charts; success tint, deliberately subtle. */
export default function HighlightsStrip({ items }: HighlightsStripProps) {
  if (items.length === 0) return null;
  return (
    <div className="flex flex-wrap items-stretch gap-3 rounded-xl border border-success-100 bg-success-50 px-4 py-3">
      {items.map((h, i) => (
        <div
          key={`${h.label}-${i}`}
          className="flex min-w-0 flex-1 basis-[12rem] items-start gap-2"
        >
          <span aria-hidden className="mt-0.5 text-success-600">
            ✦
          </span>
          <div className="min-w-0">
            <p className="text-caption uppercase tracking-wide text-success-700">{h.label}</p>
            <p className="text-body font-semibold text-text-primary truncate">{h.value}</p>
            {h.detail != null && (
              <p className="text-caption text-text-tertiary truncate">{h.detail}</p>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
