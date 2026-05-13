import { useState } from "react";
import type {
  RedistributionGroup,
  RedistributionView,
} from "@/types/supervision";

/**
 * Inline "Items redistributed" disclosure.
 *
 * Replaces the previous drawer/portal popover with a section that
 * expands directly below its trigger button, in the page flow. The
 * supervisor's question is still: "which items moved, to which
 * customers in what quantity -- and what stayed on the truck?".
 *
 * Direction is encoded by colour:
 *   - green = the customer gained units.
 *   - amber = the customer's planned share was reduced.
 *
 * When ``keptOnTruck > 0`` for a group, an explicit italic line is
 * appended under the item so the surplus that had no downstream taker
 * is visible rather than silently dropped.
 */
export interface RedistributionSectionProps {
  view: RedistributionView;
  /** Used purely to make the aria-controls id unique on the page. */
  customerCode: string;
  /** Threaded through to aria-label for screen-reader context; no UI. */
  customerName: string;
}

/** Tiny pluraliser shared with LiveSessionTab. Keeps "1 unit" / "2 units"
 *  formatting consistent without dragging in an i18n dependency. */
export function pluralise(n: number, singular: string, plural?: string): string {
  return `${n} ${n === 1 ? singular : plural ?? `${singular}s`}`;
}

export default function RedistributionSection({
  view,
  customerCode,
  customerName,
}: RedistributionSectionProps) {
  const [open, setOpen] = useState(false);

  // Drop groups that have neither a redistribution entry nor a kept-on-
  // truck remainder. Both signals are wire-supplied; the filter is a
  // pure presentation concern (don't render empty-empty rows).
  const groupsWithContent: RedistributionGroup[] = view.groups.filter(
    (g) => g.entries.length > 0 || g.keptOnTruck > 0,
  );
  const total = groupsWithContent.length;
  const sectionId = `redistribution-${customerCode}`;

  return (
    <div className="space-y-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={sectionId}
        aria-label={`View redistributions for ${customerName || customerCode}`}
        className={[
          "inline-flex items-center gap-2 rounded-md px-3 py-1.5 text-caption font-medium",
          "bg-info-100 text-info-700 hover:opacity-90 transition-colors",
        ].join(" ")}
      >
        <span>View redistributions</span>
        <svg
          className={`h-4 w-4 transition-transform ${open ? "rotate-180" : ""}`}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M19 9l-7 7-7-7"
          />
        </svg>
      </button>

      {open && (
        <div
          id={sectionId}
          className="mt-3 rounded-md border border-subtle bg-surface-raised p-4 transition-all"
        >
          {total === 0 ? (
            <p className="text-body text-text-tertiary">
              No items redistributed for this visit.
            </p>
          ) : (
            <div className="space-y-5">
              <h4 className="text-caption font-semibold uppercase tracking-wider text-text-secondary">
                Items redistributed
              </h4>
              <ul className="space-y-4">
                {groupsWithContent.map((g) => (
                  <li key={g.itemCode} className="space-y-1">
                    <div className="text-body font-medium text-text-primary">
                      {g.itemName}
                      <span className="ml-2 text-caption text-text-tertiary">
                        {g.itemCode}
                      </span>
                    </div>
                    <ul className="space-y-0.5 pl-2">
                      {g.entries.map((e) => (
                        <li
                          key={`${e.to}-${e.direction}`}
                          className={
                            e.direction === "add"
                              ? "text-body text-success-700"
                              : "text-body text-warning-700"
                          }
                        >
                          {e.toName || e.to} ({e.to}) - {pluralise(e.quantity, "unit")}
                        </li>
                      ))}
                      {g.keptOnTruck > 0 && (
                        <li className="text-body text-text-tertiary italic">
                          Kept on van - {pluralise(g.keptOnTruck, "unit")}
                        </li>
                      )}
                    </ul>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
