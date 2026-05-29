import { useState, type ReactNode } from "react";
import Modal from "./Modal";

/** Click-to-open "i" icon. Opens a Modal with `body` (rich) or `text` (plain). */
interface InfoBubbleProps {
  title: string;
  body?: ReactNode;
  text?: string;
  label?: string;
  className?: string;
}

export default function InfoBubble({
  title,
  body,
  text,
  label = "More info",
  className = "",
}: InfoBubbleProps) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          setOpen(true);
        }}
        aria-label={label}
        className={`inline-flex items-center justify-center w-4 h-4 rounded-full border border-current text-text-tertiary text-[10px] font-bold leading-none cursor-pointer select-none align-middle hover:text-brand-600 hover:border-brand-500 focus:outline-none focus:ring-2 focus:ring-brand-200 transition-colors ${className}`}
      >
        i
      </button>
      <Modal open={open} onClose={() => setOpen(false)} title={title} size="lg">
        {body ?? <p className="text-body text-text-secondary leading-relaxed">{text}</p>}
      </Modal>
    </>
  );
}
