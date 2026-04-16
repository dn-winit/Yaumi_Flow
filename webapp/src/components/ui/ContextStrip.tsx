import React, { Fragment } from "react";

interface ContextStripItem {
  label: string;
  value: React.ReactNode;
}

interface ContextStripProps {
  items: ContextStripItem[];
  actions?: React.ReactNode;
}

export default function ContextStrip({ items, actions }: ContextStripProps) {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 bg-gradient-to-r from-brand-50 to-info-50 border border-brand-100 rounded-xl px-5 py-3 text-sm">
      {items.map((item, idx) => (
        <Fragment key={`${item.label}-${idx}`}>
          {idx > 0 && <span className="text-neutral-300">|</span>}
          <span className="flex items-center gap-2">
            <span className="text-text-tertiary uppercase tracking-wide text-caption font-medium">
              {item.label}
            </span>
            {typeof item.value === "string" || typeof item.value === "number" ? (
              <span className="font-medium text-text-primary">{item.value}</span>
            ) : (
              item.value
            )}
          </span>
        </Fragment>
      ))}
      {actions && <div className="ml-auto flex items-center gap-2">{actions}</div>}
    </div>
  );
}
