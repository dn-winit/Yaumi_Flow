import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

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
 * Accessible multi-select with search and keyboard navigation.
 *
 * ARIA pattern: combobox + listbox with ``aria-activedescendant`` so screen
 * readers narrate the active row as the user arrow-keys through results
 * without moving DOM focus away from the search input. Enter toggles the
 * active option; Escape closes; Home/End jump to bounds. Empty selection
 * renders as ``placeholder`` ("All" by default).
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
  const [activeIndex, setActiveIndex] = useState(0);
  const ref = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const listboxId = useId();
  const labelId = useId();
  const optionIdPrefix = useId();

  const selected = useMemo(() => new Set(value), [value]);

  const filteredOptions = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return options;
    return options.filter(
      (o) => o.code.toLowerCase().includes(q) || o.name.toLowerCase().includes(q),
    );
  }, [options, search]);

  // Keep ``activeIndex`` clamped when the filter set shrinks below it.
  useEffect(() => {
    if (activeIndex >= filteredOptions.length) {
      setActiveIndex(Math.max(0, filteredOptions.length - 1));
    }
  }, [activeIndex, filteredOptions.length]);

  // Close on outside click / Escape so popover doesn't trap the user.
  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (!ref.current || ref.current.contains(e.target as Node)) return;
      setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const summary = useMemo(() => {
    if (loading) return "Loading…";
    if (value.length === 0) return placeholder;
    if (value.length === 1) {
      const opt = options.find((o) => o.code === value[0]);
      return opt ? labelFor(opt) : value[0];
    }
    return `${value.length} selected`;
  }, [value, options, loading, placeholder]);

  const toggle = useCallback(
    (code: string) => {
      if (selected.has(code)) {
        onChange(value.filter((v) => v !== code));
      } else {
        onChange([...value, code]);
      }
    },
    [onChange, selected, value],
  );

  function selectAll() {
    onChange(filteredOptions.map((o) => o.code));
  }

  function clear() {
    onChange([]);
  }

  const scrollActiveIntoView = useCallback((idx: number) => {
    const list = listRef.current;
    if (!list) return;
    const node = list.querySelector<HTMLElement>(`[data-option-index='${idx}']`);
    if (node) node.scrollIntoView({ block: "nearest" });
  }, []);

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Escape") {
        e.preventDefault();
        setOpen(false);
        return;
      }
      if (!filteredOptions.length) return;
      // ``true`` when the search input has typed text -- in that case
      // Home/End should defer to the browser's native caret-to-start /
      // caret-to-end behaviour rather than reposition the listbox cursor.
      // Arrow keys still navigate the listbox unconditionally (the
      // combobox pattern's expected behaviour even with caret text).
      const searchHasText = e.currentTarget.value.length > 0;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        const next = Math.min(filteredOptions.length - 1, activeIndex + 1);
        setActiveIndex(next);
        scrollActiveIntoView(next);
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        const next = Math.max(0, activeIndex - 1);
        setActiveIndex(next);
        scrollActiveIntoView(next);
      } else if (e.key === "Home" && !searchHasText) {
        e.preventDefault();
        setActiveIndex(0);
        scrollActiveIntoView(0);
      } else if (e.key === "End" && !searchHasText) {
        e.preventDefault();
        const last = filteredOptions.length - 1;
        setActiveIndex(last);
        scrollActiveIntoView(last);
      } else if (e.key === "Enter") {
        // Always preventDefault so an enclosing ``<form>`` doesn't submit
        // when the user picks an option; matches the WAI-ARIA combobox
        // pattern. If the component is ever embedded inside a form that
        // wants Enter to submit, the consumer should listen on the form
        // and route based on whether the listbox is open.
        e.preventDefault();
        const opt = filteredOptions[activeIndex];
        if (opt) toggle(opt.code);
      }
    },
    [activeIndex, filteredOptions, scrollActiveIntoView, toggle],
  );

  const activeId = filteredOptions.length > 0 ? `${optionIdPrefix}-${activeIndex}` : undefined;

  return (
    <div ref={ref} className={`relative flex flex-col gap-1 ${className}`}>
      <span
        id={labelId}
        className="text-caption font-medium text-text-tertiary uppercase tracking-wider"
      >
        {label}
      </span>
      <button
        type="button"
        disabled={disabled || loading}
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        aria-labelledby={labelId}
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
          aria-hidden="true"
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
              ref={inputRef}
              autoFocus
              type="text"
              role="combobox"
              aria-expanded="true"
              aria-controls={listboxId}
              aria-activedescendant={activeId}
              aria-autocomplete="list"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setActiveIndex(0);
              }}
              onKeyDown={onKeyDown}
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

          <div
            ref={listRef}
            id={listboxId}
            role="listbox"
            aria-multiselectable="true"
            aria-labelledby={labelId}
            className="max-h-[300px] overflow-auto"
          >
            {filteredOptions.length === 0 ? (
              <p className="px-3 py-6 text-body text-text-tertiary text-center">
                {options.length === 0 ? "No options available." : "No matches."}
              </p>
            ) : (
              filteredOptions.map((opt, idx) => {
                const checked = selected.has(opt.code);
                const isActive = idx === activeIndex;
                return (
                  <div
                    key={opt.code}
                    id={`${optionIdPrefix}-${idx}`}
                    role="option"
                    aria-selected={checked}
                    data-option-index={idx}
                    onMouseEnter={() => setActiveIndex(idx)}
                    onClick={() => toggle(opt.code)}
                    className={[
                      "flex items-center gap-2.5 px-3 py-1.5 cursor-pointer text-body",
                      isActive ? "bg-surface-sunken" : "hover:bg-surface-sunken",
                    ].join(" ")}
                  >
                    <input
                      type="checkbox"
                      tabIndex={-1}
                      checked={checked}
                      onChange={() => toggle(opt.code)}
                      onClick={(e) => e.stopPropagation()}
                      aria-hidden="true"
                      className="rounded border-strong text-brand-600 focus:ring-brand-500 pointer-events-none"
                    />
                    <span className="truncate text-text-secondary">{labelFor(opt)}</span>
                  </div>
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
