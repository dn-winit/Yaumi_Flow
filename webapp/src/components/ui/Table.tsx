import React from "react";
import EmptyState from "./EmptyState";
import { Skeleton } from "./Skeleton";

interface Column<T> {
  key: string;
  label: string;
  render?: (row: T) => React.ReactNode;
}

interface TableProps<T> {
  data: T[];
  columns: Column<T>[];
  loading?: boolean;
  emptyMessage?: string;
  onRowClick?: (row: T) => void;
  className?: string;
}

export default function Table<T extends Record<string, unknown>>({
  data,
  columns,
  loading = false,
  emptyMessage = "No data found",
  onRowClick,
  className = "",
}: TableProps<T>) {
  if (!loading && data.length === 0) {
    return <EmptyState title={emptyMessage} icon="📋" />;
  }

  return (
    <div className={`overflow-auto ${className}`}>
      <table className="w-full text-sm text-left">
        <thead className="sticky top-0 bg-surface-sunken border-b border-default">
          <tr>
            {columns.map((col) => (
              <th
                key={col.key}
                className="px-4 py-3 text-caption font-semibold text-text-secondary uppercase tracking-wider"
              >
                {col.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-subtle">
          {loading
            ? Array.from({ length: 5 }).map((_, idx) => (
                <tr key={`skeleton-${idx}`}>
                  {columns.map((col) => (
                    <td key={col.key} className="px-4 py-3">
                      <Skeleton className="h-4 w-full max-w-[140px]" />
                    </td>
                  ))}
                </tr>
              ))
            : data.map((row, idx) => (
                <tr
                  key={idx}
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  className={[
                    idx % 2 === 1 ? "bg-surface-sunken/50" : "bg-surface-raised",
                    onRowClick
                      ? "cursor-pointer hover:bg-brand-50 transition-colors"
                      : "",
                  ].join(" ")}
                >
                  {columns.map((col) => (
                    <td key={col.key} className="px-4 py-3 text-text-secondary">
                      {col.render
                        ? col.render(row)
                        : (row[col.key] as React.ReactNode)}
                    </td>
                  ))}
                </tr>
              ))}
        </tbody>
      </table>
    </div>
  );
}
