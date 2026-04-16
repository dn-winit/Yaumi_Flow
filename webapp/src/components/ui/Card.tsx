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
    "bg-surface-raised rounded-xl shadow-2 border border-default p-6",
  flat:
    "bg-surface-raised rounded-xl border border-default p-6",
  gradient:
    "bg-gradient-to-r from-brand-50 to-info-50 rounded-xl border border-brand-100 p-6",
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
        <div className="flex items-center justify-between mb-4">
          <h3 className="text-heading font-semibold text-text-primary">{title}</h3>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </div>
      )}
      {children}
    </div>
  );
}
