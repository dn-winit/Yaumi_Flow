import type { ReactNode } from "react";

/**
 * Small all-caps label that sits above a row of metric tiles or a chart
 * group. Single source of truth for the look so every page that groups
 * KPIs into themed sections renders an identical-looking heading.
 */
export default function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <h3 className="text-caption font-semibold uppercase tracking-wider text-text-tertiary">
      {children}
    </h3>
  );
}
