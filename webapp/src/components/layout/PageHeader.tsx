import React from "react";

interface PageHeaderProps {
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
  breadcrumb?: React.ReactNode;
}

export default function PageHeader({
  title,
  subtitle,
  actions,
  breadcrumb,
}: PageHeaderProps) {
  return (
    <header className="mb-4">
      {breadcrumb && <div className="mb-1">{breadcrumb}</div>}
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-heading font-semibold text-text-primary">{title}</h1>
          {subtitle && (
            <p className="text-caption text-text-tertiary mt-0.5">{subtitle}</p>
          )}
        </div>
        {actions && (
          <div className="flex items-center gap-2 shrink-0">{actions}</div>
        )}
      </div>
    </header>
  );
}
