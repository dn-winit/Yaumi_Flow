/**
 * Shared building blocks for explainability modals (forecast / recommendation).
 * One source of truth so layout & style stay identical across modals.
 */
import React from "react";

export function num(v: unknown): number | null {
  if (v == null) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export function str(v: unknown): string {
  return v == null ? "" : String(v);
}

export function SectionTitle({ children, right }: { children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between mb-2">
      <div className="text-caption font-semibold text-text-tertiary uppercase tracking-wider">
        {children}
      </div>
      {right && <div className="text-caption text-text-tertiary">{right}</div>}
    </div>
  );
}

export function Stat({
  label,
  value,
  hint,
  prose = false,
  highlight = false,
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
  /** When true, the value renders as wrapping prose (multi-line, no
   *  truncate, secondary-text weight). Use for narrative cells that hold
   *  a sentence rather than a numeric KPI -- keeps the outer Stat shell
   *  identical so the grid alignment doesn't break. */
  prose?: boolean;
  /** When true, the card draws the eye to its value: bolder left border
   *  in the brand colour and a brand-tinted, larger value. Reserved for
   *  the headline KPI of each modal (the predicted quantity in the
   *  forecast modal, the recommended quantity in the recommendation
   *  modal). Other Stats stay neutral so the highlight stands out. */
  highlight?: boolean;
}) {
  const borderClass = highlight ? "border-brand-500" : "border-brand-100";
  const valueClass = prose
    ? "mt-0.5 text-caption text-text-secondary leading-snug"
    : highlight
      ? "mt-0.5 text-lg font-bold text-brand-700 truncate"
      : "mt-0.5 text-base font-semibold text-text-primary truncate";
  return (
    <div className={`bg-surface-sunken border-l-2 ${borderClass} rounded-lg px-3 py-2.5 min-w-0`}>
      <div className="text-caption font-medium text-text-tertiary uppercase tracking-wide truncate">
        {label}
      </div>
      <div className={valueClass}>{value}</div>
      {hint && <div className="mt-0.5 text-caption text-text-tertiary leading-tight">{hint}</div>}
    </div>
  );
}

interface HeaderField {
  label: string;
  primary: string;
  secondary?: string;
}

export function ExplainHeader({ left, right }: { left: HeaderField; right: HeaderField }) {
  return (
    <div className="flex items-start justify-between gap-4 pb-3 border-b border-subtle">
      <div className="min-w-0">
        <div className="text-caption text-text-tertiary uppercase tracking-wider">{left.label}</div>
        <div className="text-base font-semibold text-text-primary truncate">{left.primary || "-"}</div>
        {left.secondary && (
          <div className="text-body text-text-secondary truncate max-w-[260px]">{left.secondary}</div>
        )}
      </div>
      <div className="text-right flex-shrink-0 min-w-0">
        <div className="text-caption text-text-tertiary uppercase tracking-wider">{right.label}</div>
        <div className="text-body font-medium text-text-primary truncate">{right.primary || "-"}</div>
        {right.secondary && (
          <div className="text-body text-text-secondary truncate max-w-[260px]">{right.secondary}</div>
        )}
      </div>
    </div>
  );
}

export const GRID_3 = "grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-2.5";
export const MODAL_BODY = "space-y-5";
