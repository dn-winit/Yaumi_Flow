import type { ReactNode } from "react";

/** All-caps section label above tile rows / chart groups. */
export default function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <h3 className="text-caption font-semibold uppercase tracking-wider text-text-tertiary">
      {children}
    </h3>
  );
}
