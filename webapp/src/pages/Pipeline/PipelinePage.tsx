import { useState, useCallback } from "react";
import PageHeader from "@/components/layout/PageHeader";
import Card from "@/components/ui/Card";
import Badge from "@/components/ui/Badge";
import Button from "@/components/ui/Button";
import KpiRow from "@/components/ui/KpiRow";
import MetricCard from "@/components/charts/MetricCard";
import Loading from "@/components/ui/Loading";
import {
  useForecastSummary,
  useTrainingSummary,
  useClassSummary,
  usePipelineStatus,
  useTriggerPipeline,
} from "@/hooks/useForecast";
import { useToast } from "@/hooks/useToast";
import AutoRetrainSection from "./AutoRetrainSection";
import { fmtNum, GOOD_SCORE_THRESHOLD } from "@/lib/format";
import type { Tone } from "@/lib/colorize";
import type { PipelineStatusResponse } from "@/types/forecast";

/* ------------------------------------------------------------------ */
/*  Step metadata                                                      */
/* ------------------------------------------------------------------ */

interface StepDef {
  key: string;
  name: string;
  description: string;
  /** Pipeline names from the API that map to this logical step. */
  pipelineKeys: string[];
}

const STEPS: StepDef[] = [
  {
    key: "collection",
    name: "Data collection",
    description: "Sales history gathered from multiple sources",
    pipelineKeys: ["data_collection", "collection"],
  },
  {
    key: "processing",
    name: "Data processing",
    description: "Cleaned, validated, and normalised",
    pipelineKeys: ["data_processing", "processing"],
  },
  {
    key: "features",
    name: "Feature engineering",
    description: "47 contextual signals built automatically",
    pipelineKeys: ["feature_engineering", "features"],
  },
  {
    key: "classification",
    name: "Demand classification",
    description: "Items grouped by buying pattern",
    pipelineKeys: ["classification", "demand_classification"],
  },
  {
    key: "training",
    name: "Model training",
    description: "Multiple models compete per item type",
    pipelineKeys: ["training", "train"],
  },
  {
    key: "forecast",
    name: "Forecast generation",
    description: "Predictions for the next 28 days",
    pipelineKeys: ["inference", "forecast"],
  },
];

const FEATURE_PILLS = ["Holidays", "Seasonality", "Lag patterns", "Rolling trends", "Route signals"];

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

type StepStatus = "completed" | "running" | "idle" | "failed";

function resolveStatus(
  step: StepDef,
  statuses: Record<string, PipelineStatusResponse> | undefined,
): { status: StepStatus; info: PipelineStatusResponse | null } {
  if (!statuses) return { status: "idle", info: null };

  // First check per-step statuses from the live pipeline run. These give
  // granular "data_collection: completed, feature_engineering: running" updates
  // while a pipeline is in progress.
  for (const pipeline of ["train", "inference"]) {
    const run = statuses[pipeline] as PipelineStatusResponse & { steps?: Record<string, string> } | undefined;
    if (!run?.steps) continue;
    for (const k of step.pipelineKeys) {
      const stepStatus = run.steps[k];
      if (stepStatus) {
        const s = stepStatus.toLowerCase();
        if (s === "completed" || s === "success") return { status: "completed", info: run };
        if (s === "running" || s === "pending") return { status: "running", info: run };
        if (s === "failed" || s === "error") return { status: "failed", info: run };
      }
    }
  }

  // Fallback: match the step against top-level pipeline statuses (for when no
  // per-step data is available — e.g. idle pipelines or old API versions).
  for (const k of step.pipelineKeys) {
    const match = statuses[k];
    if (match) {
      const s = match.status?.toLowerCase();
      if (s === "completed" || s === "success") return { status: "completed", info: match };
      if (s === "running" || s === "pending") return { status: "running", info: match };
      if (s === "failed" || s === "error") return { status: "failed", info: match };
      return { status: "idle", info: match };
    }
  }
  return { status: "idle", info: null };
}

function statusTone(s: StepStatus): Tone {
  if (s === "completed") return "success";
  if (s === "running") return "warning";
  if (s === "failed") return "danger";
  return "neutral";
}

function statusLabel(s: StepStatus): string {
  if (s === "completed") return "Completed";
  if (s === "running") return "Running";
  if (s === "failed") return "Failed";
  return "Idle";
}

function fmtTimestamp(ts: string | null): string {
  if (!ts) return "";
  try {
    const d = new Date(ts);
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return ts;
  }
}

function daysSince(ts: string | null): number | null {
  if (!ts) return null;
  try {
    const diff = Date.now() - new Date(ts).getTime();
    return Math.max(0, Math.floor(diff / 86_400_000));
  } catch {
    return null;
  }
}

/* ------------------------------------------------------------------ */
/*  Step metric sub-component                                          */
/* ------------------------------------------------------------------ */

function StepMetric({ step, summaryData, classData, trainingData }: {
  step: StepDef;
  summaryData: ReturnType<typeof useForecastSummary>["data"];
  classData: ReturnType<typeof useClassSummary>["data"];
  trainingData: ReturnType<typeof useTrainingSummary>["data"];
}) {
  switch (step.key) {
    case "collection": {
      const rows = summaryData?.total_pairs;
      return <span className="text-caption text-text-tertiary">{rows != null ? `${fmtNum(rows)} item-route pairs` : "\u2014"}</span>;
    }
    case "processing":
      return <span className="text-caption text-text-tertiary">Outliers handled, gaps filled</span>;
    case "features":
      return (
        <div className="flex flex-wrap gap-1.5 mt-1">
          {FEATURE_PILLS.map((p) => (
            <span
              key={p}
              className="inline-block text-caption px-2 py-0.5 rounded-full bg-brand-50 text-brand-700 border border-brand-100"
            >
              {p}
            </span>
          ))}
        </div>
      );
    case "classification": {
      if (!classData?.classes) return <span className="text-caption text-text-tertiary">{"\u2014"}</span>;
      const entries = Object.entries(classData.classes);
      return (
        <span className="text-caption text-text-tertiary">
          {entries.map(([cls, n]) => `${cls}: ${fmtNum(n)}`).join(" · ")}
        </span>
      );
    }
    case "training": {
      const raw = trainingData?.data as Record<string, unknown> | undefined;
      const trainedAt = raw?.trained_at ?? raw?.last_trained ?? raw?.timestamp;
      return (
        <span className="text-caption text-text-tertiary">
          {trainedAt ? `Trained ${fmtTimestamp(String(trainedAt))}` : "\u2014"}
        </span>
      );
    }
    case "forecast": {
      const count = summaryData?.future_forecast_count;
      return (
        <span className="text-caption text-text-tertiary">
          {count != null ? `${fmtNum(count)} predictions` : "\u2014"}
        </span>
      );
    }
    default:
      return null;
  }
}

/* ------------------------------------------------------------------ */
/*  Section A: Pipeline Flow                                           */
/* ------------------------------------------------------------------ */

function PipelineFlow({
  statuses,
  summaryData,
  classData,
  trainingData,
}: {
  statuses: Record<string, PipelineStatusResponse> | undefined;
  summaryData: ReturnType<typeof useForecastSummary>["data"];
  classData: ReturnType<typeof useClassSummary>["data"];
  trainingData: ReturnType<typeof useTrainingSummary>["data"];
}) {
  return (
    <Card title="Pipeline Flow">
      <div className="relative pl-10">
        {STEPS.map((step, i) => {
          const { status, info } = resolveStatus(step, statuses);
          const isLast = i === STEPS.length - 1;
          const ts = info?.finished_at ?? info?.started_at;

          return (
            <div key={step.key} className="relative pb-6 last:pb-0">
              {/* Connecting line */}
              {!isLast && (
                <div className="absolute left-[-21px] top-7 bottom-0 w-px bg-neutral-200" />
              )}

              {/* Number dot */}
              <div
                className={[
                  "absolute left-[-29px] top-0.5 flex items-center justify-center w-6 h-6 rounded-full text-caption font-bold leading-none",
                  status === "completed"
                    ? "bg-brand-600 text-white"
                    : status === "running"
                      ? "bg-brand-600 text-white animate-pulse"
                      : status === "failed"
                        ? "bg-danger-600 text-white"
                        : "bg-neutral-200 text-neutral-600",
                ].join(" ")}
              >
                {status === "completed" ? (
                  <svg className="w-3.5 h-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                  </svg>
                ) : (
                  i + 1
                )}
              </div>

              {/* Content */}
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0">
                  <h4 className="text-body font-semibold text-text-primary">{step.name}</h4>
                  <p className="text-caption text-text-secondary mt-0.5">{step.description}</p>
                  <div className="mt-1">
                    <StepMetric
                      step={step}
                      summaryData={summaryData}
                      classData={classData}
                      trainingData={trainingData}
                    />
                  </div>
                </div>
                <div className="flex flex-col items-end gap-1 shrink-0">
                  <Badge tone={statusTone(status)}>{statusLabel(status)}</Badge>
                  {ts && (
                    <span className="text-caption text-text-tertiary">{fmtTimestamp(ts)}</span>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  Section B: Model Status (merged — non-redundant with Pipeline Flow)*/
/* ------------------------------------------------------------------ */

function ModelStatus({
  summaryData,
  loading,
}: {
  summaryData: ReturnType<typeof useForecastSummary>["data"];
  loading: boolean;
}) {
  const accuracy = summaryData?.accuracy_pct;

  const ov = (summaryData as Record<string, unknown> | undefined)?.training_overview as
    | Record<string, unknown>
    | undefined;
  const trainedAt = ov?.trained_at ? String(ov.trained_at) : null;
  const trainedDays = daysSince(trainedAt);
  const testStart = ov?.test_date_start ? String(ov.test_date_start).slice(0, 10) : null;
  const testEnd = ov?.test_date_end ? String(ov.test_date_end).slice(0, 10) : null;
  const testRoutes = ov?.test_routes as number | undefined;
  const testItems = ov?.test_items as number | undefined;

  return (
    <Card title="Model status">
      <KpiRow columns={3}>
        <MetricCard
          label="Overall accuracy"
          value={accuracy != null ? `${accuracy.toFixed(1)}%` : "\u2014"}
          trend={accuracy != null ? (accuracy >= GOOD_SCORE_THRESHOLD ? "up" : "down") : undefined}
          subtitle={trainedDays != null ? `Trained ${trainedDays} day${trainedDays === 1 ? "" : "s"} ago` : "Not yet trained"}
          loading={loading}
        />
        <MetricCard
          label="Last trained"
          value={trainedAt ? fmtTimestamp(trainedAt) : "\u2014"}
          subtitle={
            testStart && testEnd
              ? `Tested on ${testStart} – ${testEnd}`
              : undefined
          }
          loading={loading}
        />
        <MetricCard
          label="Test coverage"
          value={
            testRoutes != null && testItems != null
              ? `${testRoutes} routes · ${testItems} items`
              : "\u2014"
          }
          subtitle={summaryData?.last_forecast_date ? `Forecasts through ${summaryData.last_forecast_date}` : undefined}
          loading={loading}
        />
      </KpiRow>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  Section C: Action Buttons                                          */
/* ------------------------------------------------------------------ */

type ActionState = "idle" | "loading" | "done" | "error";

function PipelineActions({ refetchStatus }: { refetchStatus: () => void }) {
  const { triggerTrain, triggerInference, loading: hookLoading, error } = useTriggerPipeline();
  const { toast } = useToast();

  const [trainState, setTrainState] = useState<ActionState>("idle");
  const [inferState, setInferState] = useState<ActionState>("idle");

  const anyBusy = hookLoading || trainState === "loading" || inferState === "loading";

  const handleTrain = useCallback(async () => {
    setTrainState("loading");
    try {
      await triggerTrain();
      setTrainState("done");
      refetchStatus();
      toast("Training started", "info");
      setTimeout(() => setTrainState("idle"), 3000);
    } catch {
      setTrainState("error");
      toast("Training failed", "danger");
      setTimeout(() => setTrainState("idle"), 4000);
    }
  }, [triggerTrain, refetchStatus, toast]);

  const handleInference = useCallback(async () => {
    setInferState("loading");
    try {
      await triggerInference();
      setInferState("done");
      refetchStatus();
      toast("Forecast generation started", "info");
      setTimeout(() => setInferState("idle"), 3000);
    } catch {
      setInferState("error");
      toast("Forecast generation failed", "danger");
      setTimeout(() => setInferState("idle"), 4000);
    }
  }, [triggerInference, refetchStatus, toast]);

  return (
    <Card title="Pipeline Actions">
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <Button
            variant="primary"
            loading={trainState === "loading"}
            disabled={anyBusy && trainState !== "loading"}
            onClick={handleTrain}
          >
            Retrain Models
          </Button>
          {trainState === "done" && (
            <span className="text-body font-medium text-success-600">Done</span>
          )}
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant="secondary"
            loading={inferState === "loading"}
            disabled={anyBusy && inferState !== "loading"}
            onClick={handleInference}
          >
            Generate Forecasts
          </Button>
          {inferState === "done" && (
            <span className="text-body font-medium text-success-600">Done</span>
          )}
        </div>
      </div>

      {error && <p className="text-body text-danger-600 mt-3">{error}</p>}
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/*  Page                                                               */
/* ------------------------------------------------------------------ */

export default function PipelinePage() {
  const { data: statuses, loading: statusLoading, refetch: refetchStatus } = usePipelineStatus();
  const { data: summaryData, loading: summaryLoading } = useForecastSummary();
  const { data: classData } = useClassSummary();
  const { data: trainingData } = useTrainingSummary();

  if (statusLoading && summaryLoading) {
    return <Loading message="Loading pipeline..." />;
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Forecasting Pipeline"
        subtitle="Monitor, control, and understand the demand prediction process."
      />

      <PipelineFlow
        statuses={statuses}
        summaryData={summaryData}
        classData={classData}
        trainingData={trainingData}
      />

      <ModelStatus summaryData={summaryData} loading={summaryLoading} />

      <PipelineActions refetchStatus={refetchStatus} />

      <AutoRetrainSection />
    </div>
  );
}
