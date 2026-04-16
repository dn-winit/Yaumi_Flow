import React from "react";
import Table from "@/components/ui/Table";
import Badge from "@/components/ui/Badge";
import type { DatasetInfo } from "@/types/data-import";

interface DatasetStatusTableProps {
  datasets: Record<string, DatasetInfo>;
}

export default function DatasetStatusTable({
  datasets,
}: DatasetStatusTableProps) {
  const rows = Object.entries(datasets).map(([name, info]) => ({
    name,
    ...info,
  }));

  const columns = [
    { key: "name", label: "Dataset" },
    {
      key: "exists",
      label: "Status",
      render: (row: (typeof rows)[number]) => (
        <Badge variant={row.exists ? "success" : "danger"}>
          {row.exists ? "Available" : "Missing"}
        </Badge>
      ),
    },
    {
      key: "rows",
      label: "Rows",
      render: (row: (typeof rows)[number]) =>
        row.rows > 0 ? row.rows.toLocaleString() : "-",
    },
    {
      key: "first_date",
      label: "First Date",
      render: (row: (typeof rows)[number]) => row.first_date ?? "-",
    },
    {
      key: "last_date",
      label: "Last Date",
      render: (row: (typeof rows)[number]) => row.last_date ?? "-",
    },
    {
      key: "size_mb",
      label: "Size",
      render: (row: (typeof rows)[number]) =>
        row.size_mb > 0 ? `${row.size_mb.toFixed(1)} MB` : "-",
    },
  ];

  return (
    <Table
      data={rows as unknown as Record<string, unknown>[]}
      columns={columns as never}
      emptyMessage="No datasets found"
    />
  );
}
