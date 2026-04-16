import React from "react";

interface DatePickerProps {
  value: string;
  onChange: (value: string) => void;
  label?: string;
  className?: string;
}

export default function DatePicker({
  value,
  onChange,
  label,
  className = "",
}: DatePickerProps) {
  return (
    <div className={`flex flex-col gap-1 ${className}`}>
      {label && (
        <label className="text-xs font-medium text-text-tertiary uppercase tracking-wider">
          {label}
        </label>
      )}
      <input
        type="date"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="block w-full rounded-lg border border-strong bg-surface-raised px-3 py-2 text-body text-text-secondary shadow-1 transition-colors focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 focus:outline-none"
      />
    </div>
  );
}
