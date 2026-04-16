import { useState } from "react";
import RecommendationModal from "./RecommendationModal";
import type { Row } from "@/types/common";

interface Props {
  row: Row;
  value: number | null | undefined;
}

export default function RecommendedValue({ row, value }: Props) {
  const [open, setOpen] = useState(false);

  if (value == null) return <span className="text-text-tertiary">-</span>;

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="font-medium text-brand-700 hover:text-brand-700 hover:underline focus:outline-none focus:ring-2 focus:ring-brand-100 rounded px-1 -mx-1"
        title="Click for recommendation explainability"
      >
        {value.toLocaleString()}
      </button>
      <RecommendationModal open={open} onClose={() => setOpen(false)} row={row} />
    </>
  );
}
