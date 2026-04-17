/**
 * Shared presentational block for AI analysis lists (strengths / weaknesses /
 * actions / root causes). Kept tiny and consistent with the rest of the app --
 * coloured card + bullet list.
 */
interface Props {
  title: string;
  items: string[];
  tone: "success" | "warning" | "danger" | "info";
}

const TONE_CLASSES: Record<Props["tone"], { bg: string; border: string; title: string; dot: string }> = {
  success: { bg: "bg-success-50", border: "border-success-100", title: "text-success-700", dot: "bg-success-500" },
  warning: { bg: "bg-warning-50", border: "border-warning-100", title: "text-warning-700", dot: "bg-warning-500" },
  danger: { bg: "bg-danger-50", border: "border-danger-100", title: "text-danger-700", dot: "bg-danger-500" },
  info: { bg: "bg-brand-50", border: "border-brand-100", title: "text-brand-700", dot: "bg-brand-500" },
};

export default function AnalysisList({ title, items, tone }: Props) {
  if (!items || items.length === 0) return null;
  const cls = TONE_CLASSES[tone];
  return (
    <div className={`${cls.bg} ${cls.border} border rounded-lg px-4 py-3`}>
      <h4 className={`text-caption font-semibold uppercase tracking-wider mb-2 ${cls.title}`}>{title}</h4>
      <ul className="space-y-1.5">
        {items.map((it, i) => (
          <li key={i} className="flex items-start gap-2 text-body text-text-secondary leading-snug">
            <span className={`mt-1.5 w-1.5 h-1.5 rounded-full shrink-0 ${cls.dot}`} />
            <span>{it}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
