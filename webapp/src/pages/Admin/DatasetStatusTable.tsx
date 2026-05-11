import Table from "@/components/ui/Table";
import Badge from "@/components/ui/Badge";
import { fmtDate } from "@/lib/date";
import { fmtNum, fmtFileSize } from "@/lib/format";
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
        row.rows > 0 ? fmtNum(row.rows) : "-",
    },
    {
      key: "first_date",
      label: "First Date",
      render: (row: (typeof rows)[number]) => fmtDate(row.first_date),
    },
    {
      key: "last_date",
      label: "Last Date",
      render: (row: (typeof rows)[number]) => fmtDate(row.last_date),
    },
    {
      key: "size_mb",
      label: "Size",
      render: (row: (typeof rows)[number]) =>
        row.size_mb > 0 ? fmtFileSize(row.size_mb) : "-",
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
