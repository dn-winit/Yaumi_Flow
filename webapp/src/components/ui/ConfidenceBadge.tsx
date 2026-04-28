import Badge from "./Badge";
import { confidenceTone } from "@/lib/colorize";
import { hasRealConfidence } from "@/lib/format";

interface Props {
  value: number | null | undefined;
  /** SBC bucket. When the class doesn't produce a real probability
   *  (smooth, erratic) the model emits a binary 0/1 fallback -- the
   *  badge renders an em-dash so the supervisor isn't shown a
   *  misleading certainty number. */
  demandClass?: string | null;
  decimals?: number;
}

export default function ConfidenceBadge({ value, demandClass, decimals = 0 }: Props) {
  if (value == null) return <Badge tone="neutral">—</Badge>;
  if (demandClass !== undefined && !hasRealConfidence(demandClass)) {
    return (
      <span title="No probability for this demand pattern">
        <Badge tone="neutral">—</Badge>
      </span>
    );
  }
  return (
    <Badge tone={confidenceTone(value)}>
      {(value * 100).toFixed(decimals)}%
    </Badge>
  );
}
