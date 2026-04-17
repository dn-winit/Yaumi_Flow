import React from "react";

interface EmptyStateProps {
  icon?: string;
  title: string;
  message?: string;
  action?: React.ReactNode;
}

export default function EmptyState({
  icon,
  title,
  message,
  action,
}: EmptyStateProps) {
  return (
    <div className="flex flex-col items-center justify-center py-12 px-4 text-center">
      {icon && (
        <span className="text-4xl text-text-tertiary mb-3" aria-hidden="true">
          {icon}
        </span>
      )}
      <h3 className="text-base font-semibold text-text-primary mb-1">{title}</h3>
      {message && <p className="text-body text-text-tertiary mb-4">{message}</p>}
      {action && <div>{action}</div>}
    </div>
  );
}
