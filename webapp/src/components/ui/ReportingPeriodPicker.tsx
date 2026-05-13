import { useState } from "react";
import DatePicker from "./DatePicker";
import { addDays, todayIso } from "@/lib/date";
import type { ReportingPeriod } from "@/types/data-import";

interface Props {
  value: ReportingPeriod;
  onChange: (next: ReportingPeriod) => void;
  /** Inclusive upper bound for any date the user can pick. Defaults to
   *  today; drawers pass ``lastActiveDate`` so the calendar disables
   *  days the data doesn't cover. ISO ``YYYY-MM-DD``. */
  maxDate?: string;
  className?: string;
}

type Mode = "single" | "range";

/**
 * Reporting-period picker. Two modes:
 *
 *   * Single day -- one date input; emits ``{start_date == end_date}``.
 *   * Date range -- two date inputs; emits ``{start_date <= end_date}``.
 *
 * The wire shape is always ``{start_date, end_date}`` (ISO ``YYYY-MM-DD``);
 * the dd-mm-yyyy display is handled by the underlying ``DatePicker``.
 *
 * Constraints are enforced at the native input level via ``min`` / ``max``
 * so the OS calendar greys out forbidden dates rather than firing onChange
 * with an invalid value:
 *   * ``end_date`` cannot precede ``start_date``  (range mode: ``min={start}``)
 *   * Neither date can exceed today                (both modes: ``max=today``)
 *
 * Switching modes preserves the user's most recent date: flipping to
 * Single day collapses to ``end_date``; flipping to Date range re-opens
 * the picker with ``start_date = end_date - 6 days`` so the user has a
 * sensible week-long starting point.
 */
export default function ReportingPeriodPicker({
  value,
  onChange,
  maxDate,
  className = "",
}: Props) {
  // Default cap is today; callers may pass a stricter ceiling (e.g.
  // lastActiveDate). Never let the cap exceed today -- past-performance
  // can't grade future days regardless of what the caller passes.
  const today = todayIso();
  const cap = maxDate && maxDate < today ? maxDate : today;
  const initialMode: Mode = value.start_date === value.end_date ? "single" : "range";
  const [mode, setMode] = useState<Mode>(initialMode);

  function switchToSingle() {
    if (mode === "single") return;
    setMode("single");
    if (value.start_date !== value.end_date) {
      onChange({ start_date: value.end_date, end_date: value.end_date });
    }
  }

  function switchToRange() {
    if (mode === "range") return;
    setMode("range");
    // Seed a week-long window so the user lands on an actually-different
    // value the moment they flip modes. Without this, the picker would
    // sit on start == end and the toggle would feel inert.
    if (value.start_date === value.end_date) {
      onChange({ start_date: addDays(value.end_date, -6), end_date: value.end_date });
    }
  }

  function setSingleDate(next: string) {
    onChange({ start_date: next, end_date: next });
  }

  function setStart(next: string) {
    // If the new start is after the current end, drag end forward so the
    // emitted value always satisfies start <= end. The DatePicker's
    // ``max`` enforces start <= today; this guard covers the cross-input
    // case the native attribute can't see.
    const end = value.end_date < next ? next : value.end_date;
    onChange({ start_date: next, end_date: end });
  }

  function setEnd(next: string) {
    onChange({ start_date: value.start_date, end_date: next });
  }

  return (
    <div className={`flex flex-col gap-1 ${className}`}>
      <label className="text-caption font-medium text-text-tertiary uppercase tracking-wider">
        Reporting period
      </label>
      <div className="flex flex-wrap items-end gap-2">
        <div
          role="tablist"
          aria-label="Reporting period mode"
          className="inline-flex items-center rounded-lg border border-strong bg-surface-raised p-0.5"
        >
          <ModeTab active={mode === "single"} onClick={switchToSingle}>
            Single day
          </ModeTab>
          <ModeTab active={mode === "range"} onClick={switchToRange}>
            Date range
          </ModeTab>
        </div>

        {mode === "single" ? (
          <DatePicker value={value.end_date} onChange={setSingleDate} max={cap} />
        ) : (
          <>
            <DatePicker
              label="From"
              value={value.start_date}
              onChange={setStart}
              max={cap}
            />
            <DatePicker
              label="To"
              value={value.end_date}
              onChange={setEnd}
              min={value.start_date}
              max={cap}
            />
          </>
        )}
      </div>
    </div>
  );
}

function ModeTab({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={
        "px-3 py-1 text-caption rounded-md transition-colors " +
        (active
          ? "bg-brand-600 text-white shadow-1"
          : "text-text-secondary hover:bg-surface-hover")
      }
    >
      {children}
    </button>
  );
}

