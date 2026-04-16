import React from "react";

interface KpiRowProps {
  children: React.ReactNode;
  columns?: 2 | 3 | 4;
}

const columnClasses: Record<2 | 3 | 4, string> = {
  2: "grid grid-cols-2 gap-4",
  3: "grid grid-cols-2 md:grid-cols-3 gap-4",
  4: "grid grid-cols-2 md:grid-cols-4 gap-4",
};

export default function KpiRow({ children, columns = 4 }: KpiRowProps) {
  return <div className={columnClasses[columns]}>{children}</div>;
}
