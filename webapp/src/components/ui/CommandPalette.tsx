import { useState, useEffect, useRef, useCallback } from "react";
import { useNavigate } from "react-router-dom";

interface Command {
  label: string;
  path: string;
  keywords: string;
}

const COMMANDS: Command[] = [
  { label: "Dashboard", path: "/", keywords: "home overview kpi" },
  { label: "Forecasting Pipeline", path: "/pipeline", keywords: "train retrain model" },
  { label: "Van Load", path: "/workflow/van-load", keywords: "forecast demand predict" },
  { label: "Recommended Orders", path: "/workflow/orders", keywords: "recommend customer" },
  { label: "Supervision", path: "/workflow/supervision", keywords: "live session visit" },
  { label: "Data Admin", path: "/admin/data", keywords: "import refresh" },
  { label: "Cache Admin", path: "/admin/cache", keywords: "clear cache" },
];

interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
}

export default function CommandPalette({ open, onClose }: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();

  const filtered = query
    ? COMMANDS.filter((cmd) => {
        const haystack = `${cmd.label} ${cmd.keywords}`.toLowerCase();
        return haystack.includes(query.toLowerCase());
      })
    : COMMANDS;

  const select = useCallback(
    (cmd: Command) => {
      navigate(cmd.path);
      onClose();
    },
    [navigate, onClose],
  );

  // Reset state when opening
  useEffect(() => {
    if (open) {
      setQuery("");
      setActiveIndex(0);
      // Focus input after the modal renders
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  // Clamp activeIndex when filtered list changes
  useEffect(() => {
    setActiveIndex((prev) => Math.min(prev, Math.max(filtered.length - 1, 0)));
  }, [filtered.length]);

  // Keyboard navigation
  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIndex((prev) => (prev + 1) % filtered.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIndex((prev) => (prev - 1 + filtered.length) % filtered.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (filtered[activeIndex]) select(filtered[activeIndex]);
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  };

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 bg-neutral-900/30 backdrop-blur-sm flex items-start justify-center pt-[15vh]"
      onClick={onClose}
    >
      <div
        className="bg-white rounded-xl shadow-2xl w-full max-w-lg mx-4 sm:mx-auto overflow-hidden"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={handleKeyDown}
      >
        {/* Search input */}
        <input
          ref={inputRef}
          type="text"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setActiveIndex(0);
          }}
          placeholder="Search pages… ⌘K"
          className="w-full px-4 py-3 text-body border-b border-neutral-100 outline-none"
        />

        {/* Results */}
        <div className="max-h-64 overflow-y-auto py-1">
          {filtered.length === 0 ? (
            <div className="px-4 py-6 text-center text-body text-neutral-400">
              No results found
            </div>
          ) : (
            filtered.map((cmd, i) => (
              <button
                key={cmd.path}
                className={[
                  "w-full text-left px-4 py-2.5 text-label cursor-pointer transition-colors",
                  i === activeIndex
                    ? "bg-brand-50 text-brand-700"
                    : "hover:bg-neutral-50 text-neutral-700",
                ].join(" ")}
                onClick={() => select(cmd)}
                onMouseEnter={() => setActiveIndex(i)}
              >
                {cmd.label}
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
