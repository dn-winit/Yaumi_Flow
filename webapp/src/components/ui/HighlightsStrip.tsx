import type { ReactNode } from "react";

/**
 * A single highlight rendered inside {@link HighlightsStrip}.
 * Value is the headline fact, detail is a short qualifier shown dimmer below.
 */
export interface Highlight {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
}

interface HighlightsStripProps {
  items: Highlight[];
}

/**
 * Positive-framed strip shown above drill-down charts in performance drawers.
 * Surfaces the "what's working" side of the dashboard so supervisors see
 * their wins alongside the gaps. Styling intentionally subtle -- success tint,
 * no loud badges -- because the numbers themselves carry the weight.
 */
export default function HighlightsStrip({ items }: HighlightsStripProps) {
  if (items.length === 0) return null;
  return (
    <div className="flex flex-wrap items-stretch gap-3 rounded-xl border border-success-100 bg-success-50 px-4 py-3">
      {items.map((h, i) => (
        <div
          key={`${h.label}-${i}`}
          className="flex min-w-0 flex-1 basis-[12rem] items-start gap-2"
        >
          <span aria-hidden className="mt-0.5 text-success-600">✦</span>
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
