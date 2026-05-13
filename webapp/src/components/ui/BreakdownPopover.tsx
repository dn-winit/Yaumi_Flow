import {
  forwardRef,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

import type { PastPerformanceItem } from "@/types/forecast";
import { fmtNum } from "@/lib/format";
import { fmtDate } from "@/lib/date";

/**
 * Numeric per-(item, date) fields on PastPerformanceItem -- the only
 * fields a breakdown popover can render in its value column. Compile-
 * time guarantee that consumers cannot pass ``itemCode`` / ``itemName``
 * / ``date`` as the value field by accident.
 */
export type BreakdownValueField =
  | "rep_van_load"
  | "past_leftover"
  | "today_allocation"
  | "recommended_van_load"
  | "recommended_carried"
  | "recommended_fresh"
  | "actual_sold"
  | "leftover_to_next_day";

export interface BreakdownPopoverProps {
  /** The clickable element -- usually the numeric value displayed on
   *  the tile. Wrapped in an inline button by the popover so the
   *  trigger remains a single interactive node. */
  trigger: ReactNode;
  /** Popover heading, e.g. "Recommended van load breakdown". */
  title: string;
  /** Sub-heading, e.g. "Window: 2026-05-04 to 2026-05-11". */
  windowLabel: string;
  /** Per-(item, date) rows. Order is the server's; the popover does
   *  not re-sort. */
  items: PastPerformanceItem[];
  /** Which PastPerformanceItem numeric field the value column reads. */
  valueField: BreakdownValueField;
  /** Canonical aggregate from the wire totals. Rendered verbatim in
   *  the footer; never recomputed from ``items``. */
  totalValue: number;
  /** Optional footer label override. Defaults to "Total". */
  totalLabel?: string;
  /** Optional value-column header override. Defaults to a humanised
   *  version of ``valueField``. */
  valueColumnLabel?: string;
}

const FIELD_LABEL: Record<BreakdownValueField, string> = {
  rep_van_load: "Actual van load",
  past_leftover: "Carried from yesterday",
  today_allocation: "Fresh from depot",
  recommended_van_load: "Recommended van load",
  recommended_carried: "Carried from yesterday",
  recommended_fresh: "Fresh from depot",
  actual_sold: "Actually sold",
  leftover_to_next_day: "Leftover to next day",
};

// ----------------------------------------------------------------------
// Layout tunables. Centralised here so layout adjustments live in one
// place and never bleed as inline magic numbers into the JSX. Pixel
// values are used because the positioner runs in raw layout coordinates
// from getBoundingClientRect; the panel's visual chrome (rounding,
// borders, typography) uses Tailwind design tokens.
// ----------------------------------------------------------------------
const PANEL_GUTTER_PX = 8;          // gap between viewport edges and trigger
const PANEL_MAX_WIDTH_PX = 520;     // anchored-panel cap on >= sm screens
const PANEL_MIN_HEIGHT_PX = 240;    // minimum visible height for the scroll body
const MOBILE_BREAKPOINT_PX = 640;   // matches Tailwind ``sm`` breakpoint

interface AnchorRect {
  top: number;
  left: number;
  width: number;
  height: number;
}

function readRect(el: HTMLElement | null): AnchorRect | null {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { top: r.top, left: r.left, width: r.width, height: r.height };
}

/**
 * Reactive viewport-width-based mobile flag. Subscribes to a
 * ``matchMedia`` query so the panel adapts to orientation changes and
 * device-mode toggles without a manual refresh. SSR-safe: defaults to
 * desktop layout when ``window`` is unavailable.
 */
function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState(() => {
    if (typeof window === "undefined") return false;
    return window.innerWidth < MOBILE_BREAKPOINT_PX;
  });
  useEffect(() => {
    if (typeof window === "undefined") return;
    const mq = window.matchMedia(
      `(max-width: ${MOBILE_BREAKPOINT_PX - 1}px)`,
    );
    const handler = (e: MediaQueryListEvent) => setIsMobile(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);
  return isMobile;
}

/**
 * Click-to-explain popover. Renders the trigger inline; on click,
 * opens a per-(item, date) breakdown table.
 *
 * Adaptive layout:
 *   * >= sm (>= 640px): anchored panel positioned next to the
 *     trigger (right side preferred, drops below if no room).
 *   * < sm: full-width bottom sheet with a dimming backdrop so the
 *     supervisor can read the table on a phone without horizontal
 *     scroll or off-screen overflow.
 *
 * Pure render: ``items`` is rendered verbatim (modulo a
 * presentation-only filter that hides rows contributing zero to the
 * displayed field), ``totalValue`` is shown in the footer as-is. The
 * only "math" the component does is presentation formatting via
 * ``fmtNum`` / ``fmtDate``.
 */
export default function BreakdownPopover({
  trigger,
  title,
  windowLabel,
  items,
  valueField,
  totalValue,
  totalLabel = "Total",
  valueColumnLabel,
}: BreakdownPopoverProps) {
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<AnchorRect | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const isMobile = useIsMobile();

  // Reposition on open + on window scroll/resize so the anchored panel
  // tracks the trigger when the surrounding drawer scrolls.
  useLayoutEffect(() => {
    if (!open || isMobile) return;
    function update() {
      setAnchor(readRect(triggerRef.current));
    }
    update();
    window.addEventListener("scroll", update, true);
    window.addEventListener("resize", update);
    return () => {
      window.removeEventListener("scroll", update, true);
      window.removeEventListener("resize", update);
    };
  }, [open, isMobile]);

  // Close on outside click / Escape so the popover never traps the user.
  // On mobile the backdrop itself is the outside-click target.
  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      const t = e.target as Node;
      if (panelRef.current && panelRef.current.contains(t)) return;
      if (triggerRef.current && triggerRef.current.contains(t)) return;
      setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Restore focus to the trigger when the popover closes so keyboard
  // users land back where they invoked it.
  const wasOpen = useRef(false);
  useEffect(() => {
    if (wasOpen.current && !open) {
      triggerRef.current?.focus();
    }
    wasOpen.current = open;
  }, [open]);

  const valueColLabel = valueColumnLabel ?? FIELD_LABEL[valueField];

  // The anchored desktop path requires a measured trigger rect before
  // mounting the panel. The mobile path is positioned via CSS only and
  // does not need anchor coordinates.
  const canRenderPanel = open && (isMobile || anchor !== null);

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-baseline rounded-md text-left underline decoration-dotted decoration-text-tertiary underline-offset-4 transition-colors hover:decoration-brand-500 hover:text-brand-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/40"
        aria-haspopup="dialog"
        aria-expanded={open}
      >
        {trigger}
      </button>
      {canRenderPanel &&
        createPortal(
          <PopoverPanel
            ref={panelRef}
            anchor={anchor}
            isMobile={isMobile}
            title={title}
            windowLabel={windowLabel}
            items={items}
            valueField={valueField}
            valueColumnLabel={valueColLabel}
            totalLabel={totalLabel}
            totalValue={totalValue}
            onClose={() => setOpen(false)}
          />,
          document.body,
        )}
    </>
  );
}

interface PanelProps {
  anchor: AnchorRect | null;
  isMobile: boolean;
  title: string;
  windowLabel: string;
  items: PastPerformanceItem[];
  valueField: BreakdownValueField;
  valueColumnLabel: string;
  totalLabel: string;
  totalValue: number;
  onClose: () => void;
}

const PopoverPanel = forwardRef<HTMLDivElement, PanelProps>(function PanelInner(
  {
    anchor,
    isMobile,
    title,
    windowLabel,
    items,
    valueField,
    valueColumnLabel,
    totalLabel,
    totalValue,
    onClose,
  },
  ref,
) {
  // Hide rows that contribute zero to this aggregate. The wire emits
  // every anchor-scoped (item, date) row carrying ALL numeric fields so
  // a single payload powers every popover; a row with
  // ``valueField === 0`` is an anchor item that didn't contribute to
  // THIS particular aggregate on THIS day. Rendering those rows would
  // be noise -- they don't sum into the footer total. This is pure
  // presentation filtering: no aggregation, no recomputation; the
  // wire's ``totalValue`` already reflects only the contributing rows.
  const visibleRows = useMemo(
    () => items.filter((row) => Number(row[valueField]) !== 0),
    [items, valueField],
  );

  // Position the desktop anchored panel. On mobile the panel is a
  // bottom sheet positioned via Tailwind classes, so we skip the JS
  // positioner entirely.
  const positionStyle = useMemo<React.CSSProperties>(() => {
    if (isMobile || !anchor || typeof window === "undefined") return {};
    const viewportW = window.innerWidth;
    const viewportH = window.innerHeight;
    let left = anchor.left + anchor.width + PANEL_GUTTER_PX;
    let top = anchor.top;
    if (left + PANEL_MAX_WIDTH_PX + PANEL_GUTTER_PX > viewportW) {
      // No room to the right -- drop below the trigger, clamped to the
      // viewport's right edge so the panel never spills off-screen.
      left = Math.max(
        PANEL_GUTTER_PX,
        Math.min(
          anchor.left,
          viewportW - PANEL_MAX_WIDTH_PX - PANEL_GUTTER_PX,
        ),
      );
      top = anchor.top + anchor.height + PANEL_GUTTER_PX;
    }
    const width = Math.min(
      PANEL_MAX_WIDTH_PX,
      viewportW - 2 * PANEL_GUTTER_PX,
    );
    const maxHeight = Math.max(
      PANEL_MIN_HEIGHT_PX,
      viewportH - top - PANEL_GUTTER_PX,
    );
    return { top, left, width, maxHeight };
  }, [isMobile, anchor]);

  const containerClasses = isMobile
    ? // Mobile: full-width bottom sheet with rounded top. ``inset-x-0
      // bottom-0`` plus a height cap keeps the table within thumb reach.
      "fixed inset-x-0 bottom-0 z-50 flex max-h-[85vh] flex-col rounded-t-2xl border-t border-strong bg-surface-raised shadow-3 animate-in slide-in-from-bottom"
    : // Desktop: anchored panel with rounded corners. ``positionStyle``
      // sets top/left/width/maxHeight via JS in this branch.
      "fixed z-50 flex flex-col rounded-lg border border-strong bg-surface-raised shadow-3";

  return (
    <>
      {isMobile && (
        <div
          onClick={onClose}
          className="fixed inset-0 z-40 bg-black/40 animate-in fade-in"
          aria-hidden="true"
        />
      )}
      <div
        ref={ref}
        role="dialog"
        aria-modal={isMobile ? "true" : undefined}
        aria-label={title}
        className={containerClasses}
        style={positionStyle}
      >
        {/* Mobile-only drag affordance: a small handle that signals the
            sheet can be dismissed. Decorative; the backdrop tap is the
            actual dismissal target. */}
        {isMobile && (
          <div className="flex justify-center pt-2" aria-hidden="true">
            <span className="h-1 w-10 rounded-full bg-border-strong" />
          </div>
        )}

        {/* Header: title + window subtitle + close button. Sticky to
            the top of the scroll body so it stays visible while the
            user scrolls long item lists. */}
        <header className="flex items-start justify-between gap-3 border-b border-subtle px-5 py-3">
          <div className="min-w-0">
            <h3 className="truncate text-body font-semibold text-text-primary">
              {title}
            </h3>
            {windowLabel && (
              <p
                className="mt-0.5 truncate text-caption text-text-tertiary"
                title={windowLabel}
              >
                {windowLabel}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="flex-shrink-0 rounded-md p-1 text-text-tertiary transition-colors hover:bg-surface-sunken hover:text-text-secondary focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/40"
          >
            <svg
              className="h-4 w-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M6 18L18 6M6 6l12 12"
              />
            </svg>
          </button>
        </header>

        {/* Scrollable body. ``min-h-0`` is required for ``flex-1`` to
            actually constrain height in a flex column. */}
        <div className="min-h-0 flex-1 overflow-auto">
          {visibleRows.length === 0 ? (
            <p className="px-5 py-10 text-center text-body text-text-tertiary">
              No items contributed to this number.
            </p>
          ) : (
            <table className="w-full border-collapse text-body">
              <thead className="sticky top-0 z-10 bg-surface-sunken">
                <tr className="border-b border-default">
                  <th className="px-4 py-2 text-left text-caption font-semibold uppercase tracking-wider text-text-secondary">
                    Date
                  </th>
                  <th className="px-4 py-2 text-left text-caption font-semibold uppercase tracking-wider text-text-secondary">
                    Item
                  </th>
                  <th className="px-4 py-2 text-right text-caption font-semibold uppercase tracking-wider text-text-secondary">
                    {valueColumnLabel}
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-subtle">
                {visibleRows.map((row) => (
                  <tr
                    key={`${row.date}-${row.itemCode}`}
                    className="transition-colors hover:bg-surface-sunken/40"
                  >
                    <td className="whitespace-nowrap px-4 py-2 align-top text-text-secondary tabular-nums">
                      {fmtDate(row.date)}
                    </td>
                    <td className="px-4 py-2 align-top">
                      <div
                        className="truncate font-medium text-text-primary"
                        title={row.itemName}
                      >
                        {row.itemName}
                      </div>
                      <div className="mt-0.5 text-caption uppercase tracking-wider text-text-tertiary">
                        {row.itemCode}
                      </div>
                    </td>
                    <td className="whitespace-nowrap px-4 py-2 text-right align-top font-medium text-text-primary tabular-nums">
                      {fmtNum(row[valueField])}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>

        {/* Sticky footer: the canonical aggregate from the wire, never
            recomputed. Stays visible while the user scrolls the table
            so they can always see what the rows sum to. */}
        <footer className="flex items-center justify-between gap-3 border-t border-default bg-surface-sunken px-5 py-3">
          <span className="text-caption font-semibold uppercase tracking-wider text-text-primary">
            {totalLabel}
          </span>
          <span className="text-body font-semibold text-text-primary tabular-nums">
            {fmtNum(totalValue)}
          </span>
        </footer>
      </div>
    </>
  );
});
