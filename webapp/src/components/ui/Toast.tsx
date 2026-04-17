import { useContext, useEffect, useRef } from "react";
import { ToastContext, type Toast } from "@/hooks/useToast";

const TONE_BORDER: Record<Toast["tone"], string> = {
  success: "border-l-success-500",
  danger: "border-l-danger-500",
  info: "border-l-brand-500",
};

function ToastItem({ t, onDismiss }: { t: Toast; onDismiss: () => void }) {
  return (
    <div
      role="status"
      className={[
        "animate-fadeIn flex items-start gap-2 rounded-lg border border-default border-l-4 bg-surface-raised px-4 py-3 shadow-3 text-body text-text-primary",
        TONE_BORDER[t.tone],
      ].join(" ")}
    >
      <span className="flex-1">{t.message}</span>
      <button
        onClick={onDismiss}
        className="shrink-0 text-text-tertiary hover:text-text-primary transition-colors duration-fast"
        aria-label="Dismiss"
      >
        &times;
      </button>
    </div>
  );
}

export default function ToastContainer() {
  const ctx = useContext(ToastContext);
  const timersRef = useRef<Map<string, number>>(new Map());

  // Clean up timers on unmount
  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      timers.forEach((timer) => clearTimeout(timer));
      timers.clear();
    };
  }, []);

  if (!ctx || ctx.toasts.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-toast flex flex-col gap-2 w-80 pointer-events-none">
      {ctx.toasts.map((t) => (
        <div key={t.id} className="pointer-events-auto">
          <ToastItem t={t} onDismiss={() => ctx.dismiss(t.id)} />
        </div>
      ))}
    </div>
  );
}
