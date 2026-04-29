import Badge from "./Badge";
import { confidenceTone } from "@/lib/colorize";
import { hasRealConfidence } from "@/lib/format";

interface Props {
  value: number | null | undefined;
  /** SBC bucket. ``smooth`` and ``erratic`` items don't produce a real
   *  per-day probability (the underlying model emits a binary 0/1
   *  fallback), so we render a *named* status chip instead of a fake
   *  percentage -- much clearer than a bare em-dash that supervisors
   *  read as "no data". */
  demandClass?: string | null;
  decimals?: number;
}

// Plain-language stand-in shown when the class doesn't emit a real
// probability. Read as "the model expects this item to sell, but the
// score isn't a meaningful per-day probability for this pattern."
const _NON_PROBABILISTIC_LABEL: Record<string, string> = {
  smooth:  "Regular",
  erratic: "Frequent",
};

export default function ConfidenceBadge({ value, demandClass, decimals = 0 }: Props) {
  if (value == null) return <Badge tone="neutral">—</Badge>;
  if (demandClass !== undefined && !hasRealConfidence(demandClass)) {
    const label = _NON_PROBABILISTIC_LABEL[
      String(demandClass ?? "").trim().toLowerCase()
    ] ?? "—";
    return (
      <span title="This buying pattern sells too consistently for a per-day probability to be meaningful — treat as a routine seller">
        <Badge tone="info">{label}</Badge>
      </span>
    );
  }
  return (
    <Badge tone={confidenceTone(value)}>
      {(value * 100).toFixed(decimals)}%
    </Badge>
  );
}
