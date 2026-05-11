import type { ReactNode } from "react";

type Variant = "error" | "success" | "warning" | "info";

interface AlertProps {
  variant: Variant;
  children: ReactNode;
  className?: string;
}

/**
 * Single source of truth for inline error / success / warning / info
 * panels. Replaces ad-hoc ``<div className="bg-danger-50 ..."``
 * blocks scattered across admin / pipeline pages so the colour ramp
 * and spacing stay in lockstep.
 */
const STYLES: Record<Variant, string> = {
  error:   "bg-danger-50  border-danger-200  text-danger-700",
  success: "bg-success-50 border-success-200 text-success-700",
  warning: "bg-warning-50 border-warning-200 text-warning-700",
  info:    "bg-brand-50   border-brand-200   text-text-secondary",
};

export default function Alert({ variant, children, className = "" }: AlertProps) {
  return (
    <div
      role={variant === "error" ? "alert" : "status"}
      className={`rounded-lg border p-4 text-body ${STYLES[variant]} ${className}`}
    >
      {children}
    </div>
  );
}
