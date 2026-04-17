import React from "react";

type CardVariant = "default" | "flat" | "gradient";

interface CardProps {
  title?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  actions?: React.ReactNode;
  variant?: CardVariant;
}

const variantClasses: Record<CardVariant, string> = {
  default:
    "bg-surface-raised rounded-xl shadow-1 p-5",
  flat:
    "bg-surface-raised rounded-xl p-5",
  gradient:
    "bg-gradient-to-r from-brand-50 to-info-50 rounded-xl border border-brand-100 p-5",
};

export default function Card({
  title,
  children,
  className = "",
  actions,
  variant = "default",
}: CardProps) {
  return (
    <div
      className={[variantClasses[variant], className]
        .filter(Boolean)
        .join(" ")}
    >
      {title && (
        <div className="flex items-center justify-between mb-4 pb-3 border-b border-subtle">
          <h3 className="text-body font-semibold text-text-primary">{title}</h3>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </div>
      )}
      {children}
    </div>
  );
}
