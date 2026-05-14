import { useEffect, useState, useCallback } from "react";
import PageHeader from "@/components/layout/PageHeader";
import Card from "@/components/ui/Card";
import Badge from "@/components/ui/Badge";
import Button from "@/components/ui/Button";
import KpiRow from "@/components/ui/KpiRow";
import MetricCard from "@/components/charts/MetricCard";
import Loading from "@/components/ui/Loading";
import {
  useForecastSummary,
  useResolvedPipelineStatus,
  useTriggerPipeline,
} from "@/hooks/useForecast";
import { useToast } from "@/hooks/useToast";
import AutoRetrainSection from "./AutoRetrainSection";
import { fmtDuration, fmtPct, GOOD_SCORE_THRESHOLD } from "@/lib/format";
import { fmtDate, fmtDateTime, daysSince } from "@/lib/date";
import InfoBubble from "@/components/ui/InfoBubble";
import ForecastAccuracyExplanation from "@/components/ui/ForecastAccuracyExplanation";
import type { Tone } from "@/lib/colorize";
import type { ResolvedPipelineStep } from "@/types/forecast";

/* ------------------------------------------------------------------ */
/*  Status -> presentation tokens (pure UI lookups, no business calc)  */
/* ------------------------------------------------------------------ */

type StepStatus = ResolvedPipelineStep["status"];

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
  if (s === "skipped") return "Skipped";
  return "Idle";
}

const fmtTimestamp = (ts: string | null): string => (ts ? fmtDateTime(ts) : "");

/* ------------------------------------------------------------------ */
/*  Step-flow visualisation                                             */
/* ------------------------------------------------------------------ */

/**
 * Connector between step ``i`` and ``i+1`` lights up brand-coloured as
 * soon as step ``i+1`` has progressed past idle.
 */
function connectorActive(next: StepStatus): boolean {
  return next === "completed" || next === "running" || next === "failed";
}

function StepCircle({ index, status }: { index: number; status: StepStatus }) {
  const isDone = status === "completed";
  const isRunning = status === "running";
  const isActive = isDone || isRunning;
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

/**
 * The ONLY client-side compute that survives this page: an animation
 * tick for the running step's elapsed-time line. Backend can't push
 * per-second updates without SSE, and the alternative (showing a
 * stale "Running for Xs" until the next status poll) feels frozen.
 */
function RunningElapsed({
  startedAt,
  lastSuccessSeconds,
}: {
  startedAt: string | null;
  lastSuccessSeconds: number | null;
}) {
  const [, setTick] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, []);
  if (!startedAt) return <>Running...</>;
  const elapsed = Math.max(0, (Date.now() - new Date(startedAt).getTime()) / 1000);
  return lastSuccessSeconds && lastSuccessSeconds > 0 ? (
    <>
      Running for {fmtDuration(elapsed)} - last run {fmtDuration(lastSuccessSeconds)}
    </>
  ) : (
    <>Running for {fmtDuration(elapsed)}</>
  );
}

function StepLabel({ step }: { step: ResolvedPipelineStep }) {
  const showRunning = step.status === "running";
  const showFailed = step.status === "failed" && Boolean(step.error);
  return (
    <div className="space-y-1">
      <h4 className="text-body font-semibold text-text-primary leading-snug">{step.name}</h4>
      <Badge tone={statusTone(step.status)}>{statusLabel(step.status)}</Badge>
      {step.metric_text && (
        <div className="text-caption text-text-secondary">{step.metric_text}</div>
      )}
      {showRunning ? (
        <div className="text-caption text-text-tertiary">
          <RunningElapsed
            startedAt={step.started_at}
            lastSuccessSeconds={step.last_success_duration_seconds}
          />
        </div>
      ) : showFailed ? (
        <div className="text-caption text-danger-600" title={step.error ?? ""}>
          {step.detail_text}
        </div>
      ) : step.detail_text ? (
        <div className="text-caption text-text-tertiary">{step.detail_text}</div>
      ) : null}
    </div>
  );
}

function PipelineFlow({ steps }: { steps: ResolvedPipelineStep[] }) {
  return (
    <>
      {/* Desktop: horizontal stepper */}
      <div className="hidden lg:flex items-start">
        {steps.map((s, i) => {
          const next = steps[i + 1];
          return (
            <div key={s.key} className="flex items-start flex-1">
              <div className="flex flex-col items-center text-center w-32 shrink-0">
                <StepCircle index={i} status={s.status} />
                <div className="mt-2">
                  <StepLabel step={s} />
                </div>
              </div>
              {next && <Connector active={connectorActive(next.status)} />}
            </div>
          );
        })}
      </div>

      {/* Tablet/mobile: vertical stepper */}
      <div className="lg:hidden flex flex-col">
        {steps.map((s, i) => {
          const next = steps[i + 1];
          return (
            <div key={s.key}>
              <div className="flex items-start gap-3">
                <StepCircle index={i} status={s.status} />
                <div className="flex-1 min-w-0 pt-1">
                  <StepLabel step={s} />
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

/* ------------------------------------------------------------------ */
/*  Header action buttons                                               */
/* ------------------------------------------------------------------ */

type ActionState = "idle" | "loading" | "done" | "error";

interface HeaderActionsProps {
  refetchStatus: () => void;
  /** Server-pushed flag -- true when any pipeline is running. */
  anyRunning: boolean;
}

function HeaderActions({ refetchStatus, anyRunning }: HeaderActionsProps) {
  const { triggerTrain, triggerInference, loading: hookLoading, error } = useTriggerPipeline();
  const { toast } = useToast();
  const [trainState, setTrainState] = useState<ActionState>("idle");
  const [inferState, setInferState] = useState<ActionState>("idle");

  const anyBusy = hookLoading || trainState === "loading" || inferState === "loading";

  const runTrigger = useCallback(
    async (
      action: "train" | "inference",
      trigger: () => Promise<{ success: boolean; message?: string | null }>,
      setState: (s: ActionState) => void,
      labels: { startedToast: string; refusedFallback: string; threwToast: string },
    ) => {
      setState("loading");
      try {
        const result = await trigger();
        if (result?.success) {
          setState("done");
          toast(labels.startedToast, "info");
        } else {
          // Server refused (guard / preflight): payload is 200 OK with
          // ``success: false`` -- show the server's reason verbatim.
          setState("error");
          toast(result?.message || labels.refusedFallback, "danger");
        }
        refetchStatus();
        setTimeout(() => setState("idle"), result?.success ? 3000 : 4000);
      } catch (err) {
        // Surface the actual error verbatim -- a swallowed catch made
        // every backend 5xx look like the same "failed to start" message
        // and hid the real cause from ops.
        const detail = err instanceof Error ? err.message : String(err ?? "");
        const msg = detail ? `${labels.threwToast}: ${detail}` : labels.threwToast;
        setState("error");
        console.error(`[pipeline.${action}]`, err);
        toast(msg, "danger");
        setTimeout(() => setState("idle"), 4000);
      }
    },
    [refetchStatus, toast],
  );

  const handleTrain = useCallback(
    () =>
      runTrigger("train", triggerTrain, setTrainState, {
        startedToast: "Training started",
        refusedFallback: "Training cannot start right now",
        threwToast: "Training failed to start",
      }),
    [runTrigger, triggerTrain],
  );

  const handleInference = useCallback(
    () =>
      runTrigger("inference", triggerInference, setInferState, {
        startedToast: "Forecast generation started",
        refusedFallback: "Forecast cannot start right now",
        threwToast: "Forecast generation failed to start",
      }),
    [runTrigger, triggerInference],
  );

  // Both buttons disable when any pipeline is running OR any local
  // trigger is in flight. The button currently spinning stays
  // interactive so its loading indicator renders.
  const disableTrain = (anyBusy || anyRunning) && trainState !== "loading";
  const disableInference = (anyBusy || anyRunning) && inferState !== "loading";

  return (
    <>
      <Button
        variant="secondary"
        size="sm"
        loading={inferState === "loading"}
        disabled={disableInference}
        onClick={handleInference}
      >
        Generate forecasts
      </Button>
      <Button
        variant="primary"
        size="sm"
        loading={trainState === "loading"}
        disabled={disableTrain}
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
  const {
    data: resolved,
    loading: statusLoading,
    error: statusError,
    refetch: refetchStatus,
  } = useResolvedPipelineStatus();
  const {
    data: summaryData,
    loading: summaryLoading,
    error: summaryError,
  } = useForecastSummary();

  // Show the spinner while EITHER query is loading. The previous
  // ``&&`` gate let one half resolve into "--" / empty stepper before
  // the other landed.
  if (statusLoading || summaryLoading) {
    return <Loading message="Loading pipeline..." />;
  }

  // Surface backend failures instead of silently rendering blanks --
  // a 5xx on resolved-status used to look identical to "no data."
  const fatalError = statusError ?? summaryError;
  if (fatalError && resolved == null) {
    return (
      <Card>
        <div className="p-6 space-y-2">
          <div className="text-h3 text-danger-700">Pipeline status unavailable</div>
          <div className="text-body text-text-secondary">{fatalError}</div>
          <Button variant="secondary" size="sm" onClick={() => refetchStatus()}>
            Retry
          </Button>
        </div>
      </Card>
    );
  }

  const accuracy = summaryData?.accuracy_pct;
  const ov = summaryData?.training_overview ?? undefined;
  const trainedAt = ov?.trained_at ? String(ov.trained_at) : null;
  const trainedDays = daysSince(trainedAt);
  const trainedAgo =
    trainedDays == null
      ? "Not yet trained"
      : trainedDays === 0
        ? "Trained today"
        : trainedDays === 1
          ? "Trained yesterday"
          : `Trained ${trainedDays} days ago`;
  const testStart = ov?.test_date_start ? fmtDate(ov.test_date_start) : null;
  const testEnd = ov?.test_date_end ? fmtDate(ov.test_date_end) : null;
  const testRoutes = ov?.test_routes ?? undefined;
  const testItems = ov?.test_items ?? undefined;
  const lastForecastDate = summaryData?.last_forecast_date
    ? fmtDate(summaryData.last_forecast_date)
    : null;

  const steps = resolved?.steps ?? [];
  const anyRunning = Boolean(resolved?.any_running);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Forecasting Pipeline"
        subtitle="Monitor, control, and understand the demand prediction process."
        actions={<HeaderActions refetchStatus={refetchStatus} anyRunning={anyRunning} />}
      />

      <KpiRow columns={3}>
        <MetricCard
          label="Baseline accuracy"
          value={accuracy != null ? fmtPct(accuracy) : "—"}
          trend={
            accuracy != null
              ? accuracy >= GOOD_SCORE_THRESHOLD ? "up" : "down"
              : undefined
          }
          subtitle={
            accuracy != null
              ? testStart && testEnd
                ? `Scored on test data ${testStart} – ${testEnd}`
                : "Scored on the held-out test data"
              : "No test predictions found — run the pipeline to score the model"
          }
          loading={summaryLoading}
          info={
            <InfoBubble
              title="How baseline accuracy is calculated"
              body={<ForecastAccuracyExplanation />}
            />
          }
        />
        <MetricCard
          label="Last trained"
          value={trainedAt ? fmtTimestamp(trainedAt) : "—"}
          subtitle={trainedAt ? trainedAgo : "Model has not been trained yet"}
          loading={summaryLoading}
          info={
            <InfoBubble
              title="When did the model last train?"
              body={
                <div className="space-y-2 text-body text-text-secondary leading-relaxed">
                  <p>The timestamp the last training run completed. Training refreshes the model with the latest sales history so forecasts stay current.</p>
                  <p>Shown in your browser's local time. The daily training cron runs on the Asia/Dubai schedule.</p>
                </div>
              }
            />
          }
        />
        <MetricCard
          label="Forecast horizon"
          value={lastForecastDate ? `Through ${lastForecastDate}` : "—"}
          subtitle={
            testRoutes != null && testItems != null
              ? `Last training tested ${testRoutes} routes · ${testItems} items`
              : "No forecast generated yet"
          }
          loading={summaryLoading}
          info={
            <InfoBubble
              title="What is the forecast horizon?"
              body={
                <div className="space-y-2 text-body text-text-secondary leading-relaxed">
                  <p>The latest date the model has predictions for. The inference cron writes new predictions daily, extending the horizon forward.</p>
                  <p>The subtitle reports the size of the held-out test slice scored at the last training (a measure of how broadly the model was evaluated, not the forward horizon itself).</p>
                </div>
              }
            />
          }
        />
      </KpiRow>

      <Card title="Pipeline flow">
        <PipelineFlow steps={steps} />
      </Card>

      <AutoRetrainSection />
    </div>
  );
}
