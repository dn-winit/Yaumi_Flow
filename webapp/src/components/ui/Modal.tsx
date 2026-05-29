import React, { useEffect, useCallback, useId, useRef, useState } from "react";
import { OVERLAY_EXIT_MS } from "@/lib/format";

type ModalSize = "sm" | "md" | "lg" | "xl";

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
  size?: ModalSize;
}

const sizeClasses: Record<ModalSize, string> = {
  sm: "max-w-sm",
  md: "max-w-md",
  lg: "max-w-2xl",
  xl: "max-w-4xl",
};

// Selectors for elements that should receive focus inside the dialog.
const FOCUSABLE_SELECTOR =
  "a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex='-1'])";

export default function Modal({ open, onClose, title, children, size = "md" }: ModalProps) {
  const [visible, setVisible] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);
  const titleId = useId();

  useEffect(() => {
    if (open) {
      setVisible(true);
    } else {
      const t = setTimeout(() => setVisible(false), OVERLAY_EXIT_MS);
      return () => clearTimeout(t);
    }
  }, [open]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        onClose();
        return;
      }
      // Focus trap on Tab: cycle focus inside the dialog panel so the
      // user cannot escape into the background page (which the
      // ``aria-hidden`` scrim is supposed to be inert to).
      if (e.key !== "Tab") return;
      const panel = panelRef.current;
      if (!panel) return;
      const focusable = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      if (focusable.length === 0) {
        e.preventDefault();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement as HTMLElement | null;
      if (e.shiftKey) {
        if (active === first || !panel.contains(active)) {
          e.preventDefault();
          last.focus();
        }
      } else {
        if (active === last) {
          e.preventDefault();
          first.focus();
        }
      }
    },
    [onClose],
  );

  // Focus management: remember what had focus before open, focus the
  // dialog panel on open, restore on close. Locks scroll on the body so
  // mobile users don't see the background scroll under the scrim.
  useEffect(() => {
    if (open) {
      previouslyFocused.current = document.activeElement as HTMLElement | null;
      document.addEventListener("keydown", handleKeyDown);
      document.body.style.overflow = "hidden";
      // Defer one frame so the panel is mounted/measurable before focus.
      requestAnimationFrame(() => {
        const panel = panelRef.current;
        if (!panel) return;
        const first = panel.querySelector<HTMLElement>(FOCUSABLE_SELECTOR);
        (first ?? panel).focus();
      });
    }
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.body.style.overflow = "";
      if (!open && previouslyFocused.current) {
        // Defer restoring focus until after React tears the modal down so
        // the previously-focused element is still in the DOM.
        const target = previouslyFocused.current;
        previouslyFocused.current = null;
        requestAnimationFrame(() => target.focus());
      }
    };
  }, [open, handleKeyDown]);

  if (!visible && !open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* Backdrop -- aria-hidden so SRs ignore it; click closes the dialog. */}
      <div
        aria-hidden="true"
        className={`absolute inset-0 bg-neutral-900/50 backdrop-blur-sm transition-opacity duration-base ${open ? "opacity-100" : "opacity-0"}`}
        onClick={onClose}
      />

      {/* Panel: proper dialog semantics for screen readers + focus trap. */}
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className={[
          "relative w-full mx-4 sm:mx-auto bg-surface-raised rounded-xl shadow-4 flex flex-col transition-all duration-base outline-none",
          open ? "scale-100 opacity-100" : "scale-95 opacity-0",
          "max-h-[90vh]",
          sizeClasses[size],
        ].join(" ")}
      >
        {/* Header (sticky) */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-subtle flex-shrink-0">
          <h2 id={titleId} className="text-heading font-semibold text-text-primary">
            {title}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="text-text-tertiary hover:text-text-secondary transition-colors rounded-lg p-1 hover:bg-surface-sunken"
          >
            <svg
              className="h-5 w-5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
              aria-hidden="true"
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Body (scrollable) */}
        <div className="px-6 py-5 overflow-y-auto flex-1">{children}</div>
      </div>
    </div>
  );
}
