import Badge from "./Badge";
import { confidenceTone } from "@/lib/colorize";

interface Props {
  value: number | null | undefined;
  decimals?: number;
}

export default function ConfidenceBadge({ value, decimals = 0 }: Props) {
  if (value == null) return <Badge tone="neutral">-</Badge>;
  const pct = `${(value * 100).toFixed(decimals)}%`;
  return <Badge tone={confidenceTone(value)}>{pct}</Badge>;
}
