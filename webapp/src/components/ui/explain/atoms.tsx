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
      <div className="text-xs font-semibold text-text-tertiary uppercase tracking-wider">
        {children}
      </div>
      {right && <div className="text-[11px] text-text-tertiary">{right}</div>}
    </div>
  );
}

export function Stat({
  label,
  value,
  hint,
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
}) {
  return (
    <div className="bg-surface-sunken rounded-lg px-3 py-2.5 border border-subtle min-w-0">
      <div className="text-[11px] font-medium text-text-tertiary uppercase tracking-wide truncate">
        {label}
      </div>
      <div className="mt-0.5 text-base font-semibold text-text-primary truncate">{value}</div>
      {hint && <div className="mt-0.5 text-[11px] text-text-tertiary leading-tight">{hint}</div>}
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
        <div className="text-[11px] text-text-tertiary uppercase tracking-wider">{left.label}</div>
        <div className="text-base font-semibold text-text-primary truncate">{left.primary || "-"}</div>
        {left.secondary && (
          <div className="text-sm text-text-secondary truncate max-w-[260px]">{left.secondary}</div>
        )}
      </div>
      <div className="text-right flex-shrink-0 min-w-0">
        <div className="text-[11px] text-text-tertiary uppercase tracking-wider">{right.label}</div>
        <div className="text-sm font-medium text-text-primary truncate">{right.primary || "-"}</div>
        {right.secondary && (
          <div className="text-sm text-text-secondary truncate max-w-[260px]">{right.secondary}</div>
        )}
      </div>
    </div>
  );
}

export const GRID_3 = "grid grid-cols-3 gap-2.5";
export const MODAL_BODY = "space-y-5";
