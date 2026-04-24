import { useMemo, useState, useCallback } from "react";
import PageHeader from "@/components/layout/PageHeader";
import Card from "@/components/ui/Card";
import Badge from "@/components/ui/Badge";
import Button from "@/components/ui/Button";
import KpiRow from "@/components/ui/KpiRow";
import MetricCard from "@/components/charts/MetricCard";
import Loading from "@/components/ui/Loading";
import {
  useForecastSummary,
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
/*  Step metadata + helpers                                            */
/* ------------------------------------------------------------------ */

interface StepDef {
  key: string;
  name: string;
  /** Pipeline names from the API that map to this logical step. */
  pipelineKeys: string[];
}

const STEPS: StepDef[] = [
  { key: "collection",     name: "Data collection",       pipelineKeys: ["data_collection", "collection"] },
  { key: "processing",     name: "Data processing",       pipelineKeys: ["data_processing", "processing"] },
  { key: "features",       name: "Feature engineering",   pipelineKeys: ["feature_engineering", "features"] },
  { key: "classification", name: "Demand classification", pipelineKeys: ["classification", "demand_classification"] },
  { key: "training",       name: "Model training",        pipelineKeys: ["training", "train"] },
  { key: "forecast",       name: "Forecast generation",   pipelineKeys: ["inference", "forecast"] },
];

type StepStatus = "completed" | "running" | "idle" | "failed";

// Single mapping from raw API status strings to our internal step state.
// Defined once so both branches of resolveStatus share it -- no duplicated
// "completed/success", "running/pending", "failed/error" handling.
const STATUS_MAP: Record<string, StepStatus> = {
  completed: "completed",
  success: "completed",
  running: "running",
  pending: "running",
  failed: "failed",
  error: "failed",
};

function mapStatus(raw: string | null | undefined): StepStatus | null {
  if (!raw) return null;
  return STATUS_MAP[raw.toLowerCase()] ?? null;
}

function resolveStatus(
  step: StepDef,
  statuses: Record<string, PipelineStatusResponse> | undefined,
): { status: StepStatus; info: PipelineStatusResponse | null } {
  if (!statuses) return { status: "idle", info: null };

  // Per-step statuses from the live pipeline run take priority -- they give
  // granular "X: completed, Y: running" updates while a pipeline is in flight.
  for (const pipeline of ["train", "inference"]) {
    const run = statuses[pipeline] as
      | (PipelineStatusResponse & { steps?: Record<string, string> })
      | undefined;
    if (!run?.steps) continue;
    for (const k of step.pipelineKeys) {
      const mapped = mapStatus(run.steps[k]);
      if (mapped) return { status: mapped, info: run };
    }
  }

  // Fallback: top-level pipeline status (idle pipelines or older API).
  for (const k of step.pipelineKeys) {
    const match = statuses[k];
    if (!match) continue;
    return { status: mapStatus(match.status) ?? "idle", info: match };
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
  if (s === "completed") return "Done";
  if (s === "running") return "Running";
  if (s === "failed") return "Failed";
  return "Idle";
}

function fmtTimestamp(ts: string | null): string {
  if (!ts) return "";
  try {
    return new Date(ts).toLocaleString(undefined, {
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
/*  Step-flow visualisation                                             */
/* ------------------------------------------------------------------ */

interface ResolvedStep {
  step: StepDef;
  status: StepStatus;
  info: PipelineStatusResponse | null;
  /** Pre-computed React node so both desktop and mobile layouts render the
   *  same memoised JSX -- avoids paying for the metric tree twice per render. */
  metric: React.ReactNode;
}

/**
 * The connector between step `i` and `i+1` lights up brand-coloured as soon
 * as step `i+1` has started (running OR completed). This makes the flow
 * "fill in" as the pipeline progresses, exactly like a wizard stepper.
 */
function connectorActive(next: StepStatus): boolean {
  return next === "completed" || next === "running";
}

function StepCircle({
  index,
  status,
}: {
  index: number;
  status: StepStatus;
}) {
  const isDone = status === "completed";
  const isRunning = status === "running";
  const isActive = isDone || isRunning; // both share the brand fill
  const isFailed = status === "failed";
  return (
    <div
      className={[
        "relative shrink-0 w-10 h-10 rounded-full flex items-center justify-center text-body font-bold leading-none",
        "transition-colors duration-base",
        isActive
          ? "bg-brand-600 text-white shadow-sm"
          : isFailed
          ? "bg-danger-600 text-white shadow-sm"
          : "bg-surface-sunken text-text-tertiary border-2 border-neutral-200",
      ].join(" ")}
    >
      {isRunning && (
        <span className="absolute inset-0 rounded-full bg-brand-600 animate-ping opacity-50" />
      )}
      {isDone ? (
        <svg className="relative w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
        </svg>
      ) : (
        <span className="relative">{index + 1}</span>
      )}
    </div>
  );
}

function Connector({ active, vertical }: { active: boolean; vertical?: boolean }) {
  if (vertical) {
    return (
      <div className="ml-5 my-1 w-0.5 h-6 self-stretch flex-shrink-0">
        <div
          className={[
            "w-full h-full rounded-full transition-colors duration-base",
            active ? "bg-brand-600" : "bg-neutral-200",
          ].join(" ")}
        />
      </div>
    );
  }
  return (
    <div className="flex-1 h-0.5 self-start mt-5 mx-1">
      <div
        className={[
          "w-full h-full rounded-full transition-colors duration-base",
          active ? "bg-brand-600" : "bg-neutral-200",
        ].join(" ")}
      />
    </div>
  );
}

function StepLabel({ resolved }: { resolved: ResolvedStep }) {
  const { step, status, info, metric } = resolved;
  const ts = info?.finished_at ?? info?.started_at;
  return (
    <div className="space-y-1">
      <h4 className="text-body font-semibold text-text-primary leading-snug">{step.name}</h4>
      <Badge tone={statusTone(status)}>{statusLabel(status)}</Badge>
      {metric && <div className="text-caption text-text-secondary">{metric}</div>}
      {ts && <div className="text-caption text-text-tertiary">{fmtTimestamp(ts)}</div>}
    </div>
  );
}

function PipelineFlow({ resolved }: { resolved: ResolvedStep[] }) {
  return (
    <>
      {/* Desktop: horizontal stepper with right-pointing connectors */}
      <div className="hidden lg:flex items-start">
        {resolved.map((r, i) => {
          const next = resolved[i + 1];
          return (
            <div key={r.step.key} className="flex items-start flex-1">
              <div className="flex flex-col items-center text-center w-32 shrink-0">
                <StepCircle index={i} status={r.status} />
                <div className="mt-2">
                  <StepLabel resolved={r} />
                </div>
              </div>
              {next && <Connector active={connectorActive(next.status)} />}
            </div>
          );
        })}
      </div>

      {/* Tablet/mobile: vertical stepper with downward connectors */}
      <div className="lg:hidden flex flex-col">
        {resolved.map((r, i) => {
          const next = resolved[i + 1];
          return (
            <div key={r.step.key}>
              <div className="flex items-start gap-3">
                <StepCircle index={i} status={r.status} />
                <div className="flex-1 min-w-0 pt-1">
                  <StepLabel resolved={r} />
                </div>
              </div>
              {next && <Connector active={connectorActive(next.status)} vertical />}
            </div>
          );
        })}
      </div>
    </>
  );
}

function StepMetric({
  step,
  summaryData,
  classData,
}: {
  step: StepDef;
  summaryData: ReturnType<typeof useForecastSummary>["data"];
  classData: ReturnType<typeof useClassSummary>["data"];
}) {
  switch (step.key) {
    case "collection": {
      const rows = summaryData?.total_pairs;
      return rows != null ? <>{fmtNum(rows)} item-route pairs</> : null;
    }
    case "processing":
      return <>Outliers handled, gaps filled</>;
    case "features":
      return <>47 contextual signals</>;
    case "classification": {
      if (!classData?.classes) return null;
      const total = Object.values(classData.classes).reduce((n, v) => n + v, 0);
      return <>{fmtNum(total)} items grouped</>;
    }
    case "training":
      return <>Best model wins per group</>;
    case "forecast": {
      const count = summaryData?.future_forecast_count;
      return count != null ? <>{fmtNum(count)} predictions</> : null;
    }
    default:
      return null;
  }
}

/* ------------------------------------------------------------------ */
/*  Header action buttons                                               */
/* ------------------------------------------------------------------ */

type ActionState = "idle" | "loading" | "done" | "error";

function HeaderActions({ refetchStatus }: { refetchStatus: () => void }) {
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
    <>
      <Button
        variant="secondary"
        size="sm"
        loading={inferState === "loading"}
        disabled={anyBusy && inferState !== "loading"}
        onClick={handleInference}
      >
        Generate forecasts
      </Button>
      <Button
        variant="primary"
        size="sm"
        loading={trainState === "loading"}
        disabled={anyBusy && trainState !== "loading"}
        onClick={handleTrain}
      >
        Retrain models
      </Button>
      {error && <span className="text-caption text-danger-600 ml-2">{error}</span>}
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  Page                                                                */
/* ------------------------------------------------------------------ */

export default function PipelinePage() {
  const { data: statuses, loading: statusLoading, refetch: refetchStatus } = usePipelineStatus();
  const { data: summaryData, loading: summaryLoading } = useForecastSummary();
  const { data: classData } = useClassSummary();

  // Resolve once per render so both desktop and mobile flow layouts share the
  // same memoised metric JSX -- fixes the "build the metric tree twice" hot path.
  const resolvedSteps = useMemo<ResolvedStep[]>(
    () =>
      STEPS.map((step) => ({
        step,
        ...resolveStatus(step, statuses),
        metric: <StepMetric step={step} summaryData={summaryData} classData={classData} />,
      })),
    [statuses, summaryData, classData]
  );

  if (statusLoading && summaryLoading) {
    return <Loading message="Loading pipeline..." />;
  }

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
    <div className="space-y-6">
      <PageHeader
        title="Forecasting Pipeline"
        subtitle="Monitor, control, and understand the demand prediction process."
        actions={<HeaderActions refetchStatus={refetchStatus} />}
      />

      {/* Top KPI strip — replaces the old stand-alone Model Status card. */}
      <KpiRow columns={3}>
        <MetricCard
          label="Overall accuracy"
          value={accuracy != null ? `${accuracy.toFixed(1)}%` : "\u2014"}
          trend={accuracy != null ? (accuracy >= GOOD_SCORE_THRESHOLD ? "up" : "down") : undefined}
          subtitle={trainedDays != null ? `Trained ${trainedDays} day${trainedDays === 1 ? "" : "s"} ago` : "Not yet trained"}
          loading={summaryLoading}
        />
        <MetricCard
          label="Last trained"
          value={trainedAt ? fmtTimestamp(trainedAt) : "\u2014"}
          subtitle={testStart && testEnd ? `Tested on ${testStart} – ${testEnd}` : undefined}
          loading={summaryLoading}
        />
        <MetricCard
          label="Forecast coverage"
          value={
            testRoutes != null && testItems != null
              ? `${testRoutes} routes · ${testItems} items`
              : "\u2014"
          }
          subtitle={summaryData?.last_forecast_date ? `Forecasts through ${summaryData.last_forecast_date}` : undefined}
          loading={summaryLoading}
        />
      </KpiRow>

      {/* Pipeline flow -- proper stepper visualisation with connectors that
          fill in as the run advances. Horizontal on desktop, vertical on
          tablet/mobile. Active step pulses; failed steps go red. */}
      <Card title="Pipeline flow">
        <PipelineFlow resolved={resolvedSteps} />
      </Card>

      <AutoRetrainSection />
    </div>
  );
}
