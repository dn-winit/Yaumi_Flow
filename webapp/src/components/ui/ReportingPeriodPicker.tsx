import { useState } from "react";
import DatePicker from "./DatePicker";
import { addDays, todayIso } from "@/lib/date";
import type { ReportingPeriod } from "@/types/data-import";

interface Props {
  value: ReportingPeriod;
  onChange: (next: ReportingPeriod) => void;
  /** Inclusive upper bound (defaults to today); drawers pass lastActiveDate. */
  maxDate?: string;
  className?: string;
}

type Mode = "single" | "range";

/** Single-day or range picker; emits {start_date, end_date} ISO with start<=end<=cap. */
export default function ReportingPeriodPicker({ value, onChange, maxDate, className = "" }: Props) {
  // Cap floored at today; callers can pass stricter (e.g. lastActiveDate).
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
    // Seed a 7-day window so the toggle feels live (otherwise start==end stays).
    if (value.start_date === value.end_date) {
      onChange({ start_date: addDays(value.end_date, -6), end_date: value.end_date });
    }
  }

  function setSingleDate(next: string) {
    onChange({ start_date: next, end_date: next });
  }

  function setStart(next: string) {
    // Drag end forward so start<=end always holds.
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
            <DatePicker label="From" value={value.start_date} onChange={setStart} max={cap} />
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
        (active ? "bg-brand-600 text-white shadow-1" : "text-text-secondary hover:bg-surface-hover")
      }
    >
      {children}
    </button>
  );
}
