import { createContext, useContext, useCallback, useState, type ReactNode } from "react";
import React from "react";

export interface Toast {
  id: string;
  message: string;
  tone: "success" | "danger" | "info";
}

interface ToastContextValue {
  toasts: Toast[];
  toast: (message: string, tone?: Toast["tone"]) => void;
  dismiss: (id: string) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

let _nextId = 0;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const dismiss = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const toast = useCallback(
    (message: string, tone: Toast["tone"] = "info") => {
      const id = `toast-${++_nextId}`;
      setToasts((prev) => {
        const next = [...prev, { id, message, tone }];
        // Max 3 visible — drop oldest
        if (next.length > 3) return next.slice(next.length - 3);
        return next;
      });

      // Auto-dismiss after 3s
      setTimeout(() => {
        dismiss(id);
      }, 3000);
    },
    [dismiss],
  );

  return React.createElement(
    ToastContext.Provider,
    { value: { toasts, toast, dismiss } },
    children,
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used within ToastProvider");
  return { toast: ctx.toast };
}

export { ToastContext };
