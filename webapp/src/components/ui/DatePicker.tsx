import { useRef } from "react";
import { fmtDate } from "@/lib/date";

interface DatePickerProps {
  /** Canonical ISO `YYYY-MM-DD`. The component never exposes the dd-mm-yyyy form to its parent. */
  value: string;
  /** Receives canonical ISO from the native calendar; never fires until a full date is picked. */
  onChange: (value: string) => void;
  label?: string;
  className?: string;
  /** Inclusive lower / upper bound, both ISO `YYYY-MM-DD`. Pass through to
   *  the native input so the OS calendar greys out forbidden dates -- much
   *  better UX than rejecting the value after the user picks it. */
  min?: string;
  max?: string;
}

/**
 * Calendar-driven date input.
 *
 * The native `<input type="date">` provides the calendar widget (its
 * value attribute is always ISO `YYYY-MM-DD`, regardless of OS locale)
 * but its rendered text varies by browser locale. We hide that native
 * surface and overlay our own `dd-mm-yyyy` chip on top so the user
 * always sees the same format the rest of the app uses, while still
 * getting the OS calendar pop-up on click. Clicking the chip / icon
 * focuses the hidden input and calls `showPicker()` on browsers that
 * support it, so a single click anywhere in the field opens the
 * calendar.
 */
export default function DatePicker({
  value,
  onChange,
  label,
  className = "",
  min,
  max,
}: DatePickerProps) {
  const inputRef = useRef<HTMLInputElement>(null);

  const openPicker = () => {
    const el = inputRef.current;
    if (!el) return;
    // showPicker() is the supported way to open the OS calendar
    // programmatically (Chrome 99+, Edge, Safari 17+). When unavailable
    // we fall back to focus(), which still triggers the popup on
    // user-initiated clicks in every modern browser.
    if (typeof el.showPicker === "function") {
      try {
        el.showPicker();
        return;
      } catch {
        /* falls through to focus() */
      }
    }
    el.focus();
  };

  return (
    <div className={`flex flex-col gap-1 ${className}`}>
      {label && (
        <label className="text-caption font-medium text-text-tertiary uppercase tracking-wider">
          {label}
        </label>
      )}
      <div
        className="relative cursor-pointer rounded-lg border border-strong bg-surface-raised shadow-1 transition-colors focus-within:border-brand-500 focus-within:ring-2 focus-within:ring-brand-500/20"
        onClick={openPicker}
      >
        <input
          ref={inputRef}
          type="date"
          value={value}
          min={min}
          max={max}
          onChange={(e) => {
            const next = e.target.value;
            // Native picker only fires onChange with a full ISO date or
            // an empty string (when the user clears it). We ignore the
            // clear since every caller expects a non-empty date.
            if (next && next !== value) onChange(next);
          }}
          aria-label={label ?? "Select date"}
          className="absolute inset-0 h-full w-full cursor-pointer opacity-0"
        />
        <div className="pointer-events-none flex items-center justify-between gap-2 px-3 py-2">
          <span className="text-body text-text-secondary">
            {value ? fmtDate(value) : <span className="text-text-tertiary">dd-mm-yyyy</span>}
          </span>
          <CalendarIcon />
        </div>
      </div>
    </div>
  );
}

function CalendarIcon() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      className="h-4 w-4 text-text-tertiary"
    >
      <rect x="3" y="5" width="18" height="16" rx="2" />
      <path strokeLinecap="round" d="M3 10h18M8 3v4M16 3v4" />
    </svg>
  );
}
