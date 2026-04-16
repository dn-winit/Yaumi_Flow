import { useMemo } from "react";
import Table from "@/components/ui/Table";
import PredictedValue from "@/components/ui/PredictedValue";
import ConfidenceBadge from "@/components/ui/ConfidenceBadge";
import { toNum } from "@/lib/format";
import type { Row } from "@/types/common";

interface Props {
  rows: Row[];
}

export default function VanLoadTable({ rows }: Props) {
  const sorted = useMemo(() => {
    return [...rows].sort(
      (a, b) => (toNum(b.prediction) ?? 0) - (toNum(a.prediction) ?? 0)
    );
  }, [rows]);

  const columns = [
    {
      key: "ItemCode",
      label: "Item Code",
      render: (r: Row) => (
        <span className="font-medium text-text-primary">
          {String(r.ItemCode ?? "-")}
        </span>
      ),
    },
    {
      key: "ItemName",
      label: "Item Name",
      render: (r: Row) => String(r.item_name ?? r.ItemName ?? "-"),
    },
    {
      key: "Predicted",
      label: "Recommended qty",
      render: (r: Row) => <PredictedValue row={r} value={toNum(r.prediction)} />,
    },
    {
      key: "Confidence",
      label: "Chance of selling",
      render: (r: Row) => <ConfidenceBadge value={toNum(r.p_demand)} />,
    },
    {
      key: "Range",
      label: "Likely range (low-high)",
      render: (r: Row) => {
        const lo = toNum(r.q_10);
        const hi = toNum(r.q_90);
        if (lo == null || hi == null) return "-";
        return `${lo.toFixed(1)} - ${hi.toFixed(1)}`;
      },
    },
  ];

  return (
    <Table
      data={sorted}
      columns={columns}
      emptyMessage="No items to load"
    />
  );
}
