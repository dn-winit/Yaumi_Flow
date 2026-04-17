import Card from "@/components/ui/Card";
import Badge from "@/components/ui/Badge";
import Select from "@/components/ui/Select";
import {
  useRetrainConfig,
  useRetrainHistory,
  useUpdateRetrainConfig,
} from "@/hooks/useForecast";
import type { Tone } from "@/lib/colorize";

/* ------------------------------------------------------------------ */
/*  Toggle switch (pure CSS)                                           */
/* ------------------------------------------------------------------ */

function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className="flex items-center gap-2 cursor-pointer bg-transparent border-none p-0"
    >
      <span
        className={[
          "relative inline-flex h-5 w-9 shrink-0 rounded-full transition-colors duration-200",
          checked ? "bg-brand-600" : "bg-neutral-300",
        ].join(" ")}
      >
        <span
          className={[
            "absolute top-0.5 left-0.5 inline-block h-4 w-4 rounded-full bg-white shadow transition-transform duration-200",
            checked ? "translate-x-4" : "translate-x-0",
          ].join(" ")}
        />
      </span>
      <span className="text-body text-text-secondary">{label}</span>
    </button>
  );
}

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

const FREQ_OPTIONS = [
  { value: "7", label: "Every 7 days" },
  { value: "14", label: "Every 14 days" },
  { value: "21", label: "Every 21 days" },
  { value: "30", label: "Every 30 days" },
];

function driftTone(status: string): Tone {
  if (status === "significant") return "danger";
  if (status === "drifting") return "warning";
  return "success";
}

function driftLabel(status: string): string {
  if (status === "significant") return "Significant drift";
  if (status === "drifting") return "Drifting";
  return "Stable";
}

function fmtDate(iso: string | null): string {
  if (!iso) return "\u2014";
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
      year: "numeric",
    });
  } catch {
    return iso;
  }
}

function fmtShortDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    });
  } catch {
    return iso;
  }
}

function timeAgo(iso: string | null): string {
  if (!iso) return "";
  try {
    const days = Math.floor((Date.now() - new Date(iso).getTime()) / 86_400_000);
    if (days === 0) return "(today)";
    if (days === 1) return "(1 day ago)";
    return `(${days} days ago)`;
  } catch {
    return "";
  }
}

function fmtPct(v: number | null): string {
  if (v == null) return "\u2014";
  return `${v.toFixed(1)}%`;
}

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

export default function AutoRetrainSection() {
  const { data: config, loading: configLoading } = useRetrainConfig();
  const { data: history } = useRetrainHistory();
  const updateMutation = useUpdateRetrainConfig();

  const enabled = config?.enabled ?? false;
  const freqDays = config?.frequency_days ?? 14;
  const autoInference = config?.auto_inference_after_train ?? true;
  const drift = config?.drift;

  const handleToggle = (v: boolean) => updateMutation.mutate({ enabled: v });
  const handleFreq = (v: string) => updateMutation.mutate({ frequency_days: Number(v) });
  const handleAutoInference = (v: boolean) =>
    updateMutation.mutate({ auto_inference_after_train: v });

  const recentHistory = (history ?? []).slice(0, 5);

  return (
    <Card title="Auto-retrain schedule">
      {/* Controls row */}
      <div className="flex flex-wrap items-center gap-6 mb-6">
        <Toggle
          checked={enabled}
          onChange={handleToggle}
          label="Enable auto-retrain"
        />

        <Select
          value={String(freqDays)}
          onChange={handleFreq}
          options={FREQ_OPTIONS}
          className="w-44"
        />

        <label className="flex items-center gap-2 cursor-pointer text-body text-text-secondary">
          <input
            type="checkbox"
            checked={autoInference}
            onChange={(e) => handleAutoInference(e.target.checked)}
            className="h-4 w-4 rounded border-strong text-brand-600 focus:ring-brand-500"
          />
          Auto-generate forecasts after retrain
        </label>
      </div>

      {/* Status panel */}
      <div className="rounded-lg border border-default bg-surface-sunken p-4 mb-6">
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {/* Drift status */}
          <div>
            <span className="text-caption text-text-tertiary flex items-center gap-1 mb-1">
              Drift status
              <span
                className="inline-flex items-center justify-center w-3.5 h-3.5 rounded-full border border-default text-[9px] font-semibold text-text-tertiary cursor-help"
                title="Monitors how well predictions match real sales over the last 7 days. If performance drops, you'll see a warning here."
              >
                i
              </span>
            </span>
            {drift ? (
              <Badge tone={driftTone(drift.status)}>{driftLabel(drift.status)}</Badge>
            ) : configLoading ? (
              <span className="text-body text-text-tertiary">Loading...</span>
            ) : (
              <Badge tone="neutral">Unknown</Badge>
            )}
          </div>

          {/* Recent vs baseline accuracy */}
          <div>
            <span className="text-caption text-text-tertiary flex items-center gap-1 mb-1">
              Recent accuracy
              <span
                className="inline-flex items-center justify-center w-3.5 h-3.5 rounded-full border border-default text-[9px] font-semibold text-text-tertiary cursor-help"
                title="How closely our predictions matched what customers actually bought in the last 7 days, compared to the accuracy when the model was trained."
              >
                i
              </span>
            </span>
            <span className="text-body font-semibold text-text-primary">
              {fmtPct(drift?.recent_accuracy ?? null)}
            </span>
            {drift?.baseline_accuracy != null && (
              <span className="text-caption text-text-tertiary ml-2">
                vs {fmtPct(drift.baseline_accuracy)} at training
              </span>
            )}
            {drift?.delta != null && (
              <span
                className={`text-caption ml-2 ${
                  drift.delta >= 0 ? "text-success-600" : "text-danger-600"
                }`}
              >
                {drift.delta >= 0 ? "\u0394 +" : "\u0394 "}
                {drift.delta.toFixed(1)}
              </span>
            )}
          </div>

          {/* Next scheduled */}
          <div>
            <span className="text-caption text-text-tertiary block mb-1">
              Next auto-retrain
            </span>
            <span className="text-body font-semibold text-text-primary">
              {enabled ? fmtDate(config?.next_scheduled ?? null) : "Disabled"}
            </span>
          </div>

          {/* Last auto-retrain */}
          <div>
            <span className="text-caption text-text-tertiary block mb-1">
              Last auto-retrain
            </span>
            <span className="text-body font-semibold text-text-primary">
              {fmtDate(config?.last_auto_retrain ?? null)}
            </span>
            {config?.last_auto_retrain && (
              <span className="text-caption text-text-tertiary ml-1">
                {timeAgo(config.last_auto_retrain)}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* History table */}
      {recentHistory.length > 0 && (
        <div>
          <h4 className="text-caption font-semibold text-text-secondary uppercase tracking-wider mb-2">
            History (last {recentHistory.length})
          </h4>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-body">
              <thead>
                <tr className="border-b border-default">
                  <th className="py-2 pr-4 text-caption font-semibold text-text-tertiary">Date</th>
                  <th className="py-2 pr-4 text-caption font-semibold text-text-tertiary">Trigger</th>
                  <th className="py-2 pr-4 text-caption font-semibold text-text-tertiary">Before</th>
                  <th className="py-2 pr-4 text-caption font-semibold text-text-tertiary">After</th>
                  <th className="py-2 text-caption font-semibold text-text-tertiary">Status</th>
                </tr>
              </thead>
              <tbody>
                {recentHistory.map((h, i) => (
                  <tr key={i} className="border-b border-default last:border-b-0">
                    <td className="py-2 pr-4 text-text-secondary">{fmtShortDate(h.date)}</td>
                    <td className="py-2 pr-4 text-text-secondary capitalize">{h.trigger}</td>
                    <td className="py-2 pr-4 text-text-secondary">{fmtPct(h.accuracy_before)}</td>
                    <td className="py-2 pr-4 text-text-secondary">{fmtPct(h.accuracy_after)}</td>
                    <td className="py-2">
                      <Badge tone={h.status === "success" ? "success" : "danger"}>
                        {h.status === "success" ? "Success" : "Failed"}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {recentHistory.length === 0 && !configLoading && (
        <p className="text-caption text-text-tertiary">No auto-retrain history yet.</p>
      )}
    </Card>
  );
}
