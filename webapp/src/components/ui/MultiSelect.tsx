import { useEffect, useMemo, useRef, useState } from "react";

export interface MultiSelectOption {
  code: string;
  name: string;
}

interface Props {
  label: string;
  options: MultiSelectOption[];
  value: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  loading?: boolean;
  disabled?: boolean;
  className?: string;
  // Width of the trigger button. Popover is anchored to it.
  triggerWidth?: string;
}

/**
 * Popover-style multi-select. Trigger shows a compact chip-count summary;
 * the popover opens below the trigger with a search box, "Select all" /
 * "Clear" actions, and code-name rows. Empty selection is rendered as
 * "All" — semantically, no filter applied.
 */
export default function MultiSelect({
  label,
  options,
  value,
  onChange,
  placeholder = "All",
  loading = false,
  disabled = false,
  className = "",
  triggerWidth = "min-w-[220px]",
}: Props) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const ref = useRef<HTMLDivElement | null>(null);

  // Close on outside click / Escape so popover doesn't trap the user.
  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (!ref.current || ref.current.contains(e.target as Node)) return;
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

  const selected = useMemo(() => new Set(value), [value]);

  const filteredOptions = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return options;
    return options.filter(
      (o) =>
        o.code.toLowerCase().includes(q) ||
        o.name.toLowerCase().includes(q)
    );
  }, [options, search]);

  const summary = useMemo(() => {
    if (loading) return "Loading…";
    if (value.length === 0) return placeholder;
    if (value.length === 1) {
      const opt = options.find((o) => o.code === value[0]);
      return opt ? labelFor(opt) : value[0];
    }
    return `${value.length} selected`;
  }, [value, options, loading, placeholder]);

  function toggle(code: string) {
    if (selected.has(code)) {
      onChange(value.filter((v) => v !== code));
    } else {
      onChange([...value, code]);
    }
  }

  function selectAll() {
    onChange(filteredOptions.map((o) => o.code));
  }

  function clear() {
    onChange([]);
  }

  return (
    <div ref={ref} className={`relative flex flex-col gap-1 ${className}`}>
      <label className="text-caption font-medium text-text-tertiary uppercase tracking-wider">
        {label}
      </label>
      <button
        type="button"
        disabled={disabled || loading}
        onClick={() => setOpen((v) => !v)}
        className={[
          "flex items-center justify-between gap-2 rounded-lg border bg-surface-raised px-3 py-2 text-body shadow-1 transition-colors",
          triggerWidth,
          disabled || loading
            ? "border-default text-text-tertiary cursor-not-allowed"
            : open
            ? "border-brand-500 text-text-primary ring-2 ring-brand-500/20"
            : "border-strong text-text-secondary hover:border-brand-400",
        ].join(" ")}
      >
        <span className="truncate text-left">{summary}</span>
        <svg
          width="14"
          height="14"
          viewBox="0 0 20 20"
          fill="none"
          className="flex-shrink-0 text-text-tertiary"
        >
          <path
            d="M5 8l5 5 5-5"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </button>

      {open && (
        <div className="absolute left-0 top-full z-30 mt-1 w-[320px] max-w-[90vw] rounded-lg border border-strong bg-surface-raised shadow-3">
          <div className="p-2 border-b border-subtle">
            <input
              autoFocus
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder={`Search ${label.toLowerCase()}…`}
              className="w-full rounded-md border border-default bg-surface-base px-2.5 py-1.5 text-body text-text-primary focus:border-brand-500 focus:ring-1 focus:ring-brand-500/30 focus:outline-none"
            />
          </div>

          <div className="flex items-center justify-between px-3 py-1.5 border-b border-subtle text-caption">
            <span className="text-text-tertiary">
              {value.length} of {options.length} selected
            </span>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={selectAll}
                disabled={filteredOptions.length === 0}
                className="font-semibold text-brand-600 hover:text-brand-700 disabled:text-text-tertiary disabled:cursor-not-allowed"
              >
                {search ? "Select shown" : "Select all"}
              </button>
              <button
                type="button"
                onClick={clear}
                disabled={value.length === 0}
                className="font-semibold text-text-tertiary hover:text-text-secondary disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Clear
              </button>
            </div>
          </div>

          <div className="max-h-[300px] overflow-auto">
            {filteredOptions.length === 0 ? (
              <p className="px-3 py-6 text-body text-text-tertiary text-center">
                {options.length === 0 ? "No options available." : "No matches."}
              </p>
            ) : (
              filteredOptions.map((opt) => {
                const checked = selected.has(opt.code);
                return (
                  <label
                    key={opt.code}
                    className="flex items-center gap-2.5 px-3 py-1.5 hover:bg-surface-sunken cursor-pointer text-body"
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggle(opt.code)}
                      className="rounded border-strong text-brand-600 focus:ring-brand-500"
                    />
                    <span className="truncate text-text-secondary">
                      {labelFor(opt)}
                    </span>
                  </label>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function labelFor(opt: MultiSelectOption): string {
  if (!opt.name || opt.name === opt.code) return opt.code;
  return `${opt.code} — ${opt.name}`;
}
