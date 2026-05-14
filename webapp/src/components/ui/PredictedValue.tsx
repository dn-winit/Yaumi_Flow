import { useState } from "react";
import ExplainabilityModal from "./ExplainabilityModal";
import { fmtNum } from "@/lib/format";
import type { Row } from "@/types/common";

interface Props {
  row: Row;
  value: number | null | undefined;
}

export default function PredictedValue({ row, value }: Props) {
  const [open, setOpen] = useState(false);

  if (value == null) return <span className="text-text-tertiary">-</span>;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="font-medium text-brand-700 hover:text-brand-700 hover:underline focus:outline-none focus:ring-2 focus:ring-brand-100 rounded px-1 -mx-1"
        title="Click to see why we forecast this"
      >
        {fmtNum(value)}
      </button>
      <ExplainabilityModal open={open} onClose={() => setOpen(false)} row={row} />
    </>
  );
}
