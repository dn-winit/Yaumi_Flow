import { useEffect, useMemo, useState, useCallback, type ReactNode } from "react";
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
import { fmtDate, fmtDateTime } from "@/lib/date";
import InfoBubble from "@/components/ui/InfoBubble";
import ForecastAccuracyExplanation from "@/components/ui/ForecastAccuracyExplanation";
import type { Tone } from "@/lib/colorize";
import type { PipelineStatusResponse, PipelineCascade } from "@/types/forecast";

/* ------------------------------------------------------------------ */
/*  Step metadata + helpers                                            */
/* ------------------------------------------------------------------ */

interface StepDef {
  key: string;
  name: string;
  /** Pipeline step keys (from on_step callbacks) that map to this logical step. */
  pipelineKeys: string[];
}

// One row per phase the supervisor cares about. The publishing row
// rolls up the cascade we wired in pipeline_service (db_push +
// data_import_refresh) so the user can see whether downstream apps
// got the new numbers.
const STEPS: StepDef[] = [
  { key: "collection",     name: "Data collection",        pipelineKeys: ["data_collection", "collection"] },
  { key: "processing",     name: "Data processing",        pipelineKeys: ["data_processing", "processing"] },
  { key: "classification", name: "Demand classification",  pipelineKeys: ["classification", "demand_classification"] },
  { key: "features",       name: "Feature engineering",    pipelineKeys: ["feature_engineering", "features"] },
  { key: "training",       name: "Model training",         pipelineKeys: ["training", "train"] },
  { key: "forecast",       name: "Forecast generation",    pipelineKeys: ["inference", "forecast"] },
  { key: "publish",        name: "Publishing results",     pipelineKeys: ["db_push", "data_import_refresh"] },
];

type StepStatus = "completed" | "running" | "idle" | "failed" | "skipped";

const STATUS_MAP: Record<string, StepStatus> = {
  completed: "completed",
  success: "completed",
  running: "running",
  pending: "running",
  failed: "failed",
  error: "failed",
  skipped: "skipped",
};

function mapStatus(raw: string | null | undefined): StepStatus | null {
  if (!raw) return null;
  return STATUS_MAP[raw.toLowerCase()] ?? null;
}

/**
 * For a regular step, walk the pipelines and return the first matching
 * step status. The publishing step uses ``resolvePublishStatus`` instead
 * because it has to combine two sub-step states (worst wins).
 */
function resolveStatus(
  step: StepDef,
  statuses: Record<string, PipelineStatusResponse> | undefined,
): { status: StepStatus; info: PipelineStatusResponse | null } {
  if (!statuses) return { status: "idle", info: null };

  for (const pipeline of ["train", "inference"]) {
    const run = statuses[pipeline];
    if (!run?.steps) continue;
    for (const k of step.pipelineKeys) {
      const mapped = mapStatus(run.steps[k]);
      if (mapped) return { status: mapped, info: run };
    }
  }

  // Fallback: top-level pipeline status (idle pipelines).
  for (const k of step.pipelineKeys) {
    const match = statuses[k];
    if (!match) continue;
    return { status: mapStatus(match.status) ?? "idle", info: match };
  }
  return { status: "idle", info: null };
}

// Aggregate priority for the publishing step's two sub-steps.
const PUBLISH_PRIORITY: Record<StepStatus, number> = {
  failed: 4,
  running: 3,
  completed: 2,
  skipped: 1,
  idle: 0,
};

function resolvePublishStatus(
  statuses: Record<string, PipelineStatusResponse> | undefined,
): { status: StepStatus; info: PipelineStatusResponse | null; cascade: PipelineCascade | null } {
  if (!statuses) return { status: "idle", info: null, cascade: null };

  // The most recent run (by started_at) owns the publishing display so
  // the page reflects the latest activity even when both pipelines have
  // history.
  const candidates = (["train", "inference"] as const)
    .map((p) => statuses[p])
    .filter((r): r is PipelineStatusResponse => Boolean(r?.started_at))
    .sort(
      (a, b) =>
        new Date(b.started_at ?? 0).getTime() - new Date(a.started_at ?? 0).getTime(),
    );

  if (candidates.length === 0) return { status: "idle", info: null, cascade: null };
  const latest = candidates[0];
  const subKeys = ["db_push", "data_import_refresh"] as const;
  const subStatuses = subKeys.map((k) => mapStatus(latest.steps?.[k]) ?? "idle");

  const status = subStatuses.reduce<StepStatus>((worst, s) => {
    return PUBLISH_PRIORITY[s] > PUBLISH_PRIORITY[worst] ? s : worst;
  }, "idle");

  return {
    status,
    info: latest,
    cascade: (latest.result?.cascade as PipelineCascade | undefined) ?? null,
  };
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
  if (s === "skipped") return "Skipped";
  return "Idle";
}

const fmtTimestamp = (ts: string | null): string => (ts ? fmtDateTime(ts) : "");

/** "1m 14s" / "8m 42s" / "47s". Single source of truth for duration text. */
function fmtDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "0s";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return s > 0 ? `${m}m ${s}s` : `${m}m`;
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
  /** Plain-language metric line shown under the status badge. */
  metric: ReactNode;
  /** Plain-language detail line (timestamp / live duration / error / cascade summary). */
  detail: ReactNode;
}

/**
 * Connector between step `i` and `i+1` lights up brand-coloured as soon
 * as step `i+1` has progressed past idle.
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

function StepLabel({ resolved }: { resolved: ResolvedStep }) {
  const { step, status, metric, detail } = resolved;
  return (
    <div className="space-y-1">
      <h4 className="text-body font-semibold text-text-primary leading-snug">{step.name}</h4>
      <Badge tone={statusTone(status)}>{statusLabel(status)}</Badge>
      {metric && <div className="text-caption text-text-secondary">{metric}</div>}
      {detail && <div className="text-caption text-text-tertiary">{detail}</div>}
    </div>
  );
}

function PipelineFlow({ resolved }: { resolved: ResolvedStep[] }) {
  return (
    <>
      {/* Desktop: horizontal stepper */}
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

      {/* Tablet/mobile: vertical stepper */}
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

/* ------------------------------------------------------------------ */
/*  Per-step metric + detail builders                                  */
/* ------------------------------------------------------------------ */

interface StepCtx {
  step: StepDef;
  status: StepStatus;
  info: PipelineStatusResponse | null;
  publishCascade: PipelineCascade | null;
  summary: ReturnType<typeof useForecastSummary>["data"];
  classData: ReturnType<typeof useClassSummary>["data"];
  /** Tick that increments every second while a pipeline is running. Used as a render trigger. */
  liveTick: number;
}

/**
 * Plain-language metric for each phase. Returns null when we have no
 * confident data to surface -- never a guess, never a hardcoded number.
 */
function stepMetric(ctx: StepCtx): ReactNode {
  const { step, summary, classData, publishCascade, status } = ctx;
  const ov = (summary as Record<string, unknown> | undefined)?.training_overview as
    | Record<string, unknown>
    | undefined;

  switch (step.key) {
    case "collection": {
      // ``total_pairs`` is null until data_processing finishes; routes
      // come from training_overview which only populates after training.
      // Treat 0 as "no honest count" so the chip stays empty rather than
      // claiming "0 item-route pairs" mid-run.
      const pairs = summary?.total_pairs;
      const routes = ov?.test_routes as number | undefined;
      const showPairs = pairs != null && pairs > 0;
      const showRoutes = routes != null && routes > 0;
      if (!showPairs && !showRoutes) return null;
      const parts: string[] = [];
      if (showPairs) parts.push(`${fmtNum(pairs)} item-route pairs`);
      if (showRoutes) parts.push(`${fmtNum(routes)} routes`);
      return parts.join(" · ");
    }
    case "processing": {
      const start = ov?.test_date_start ? fmtDate(ov.test_date_start) : null;
      const end = ov?.test_date_end ? fmtDate(ov.test_date_end) : null;
      if (!start || !end) return null;
      return `Reviewed ${start} – ${end}`;
    }
    case "classification": {
      const classes = classData?.classes ?? summary?.classes;
      if (!classes) return null;
      const total = Object.values(classes).reduce<number>((n, v) => n + Number(v ?? 0), 0);
      if (total === 0) return null;
      return `${fmtNum(total)} items grouped by buying pattern`;
    }
    case "features": {
      const feats = ov?.feature_count as number | undefined;
      if (feats == null || feats === 0) return null;
      return `${fmtNum(feats)} predictive signals built`;
    }
    case "training": {
      const total = ov?.total_models_trained as number | undefined;
      const winners = ov?.class_winners as
        | Array<{ demand_class?: string; best_model?: string }>
        | undefined;
      if (total == null || total === 0) return null;
      const picks = Array.isArray(winners) ? winners.length : 0;
      // Plain language: avoid "WAPE" (unknown to non-technical readers)
      // and the single-class "best" number that was misleadingly rosy
      // (smooth class WAPE != lumpy class WAPE). The Overall accuracy
      // KPI above already gives the headline number.
      return picks > 0
        ? `${fmtNum(total)} models compared · best picked for each of ${picks} patterns`
        : `${fmtNum(total)} models compared`;
    }
    case "forecast": {
      // future_forecast_count is 0 until inference writes its CSV; show
      // nothing rather than "0 predictions" while training is still running.
      const count = summary?.future_forecast_count;
      const last = summary?.last_forecast_date ? fmtDate(summary.last_forecast_date) : null;
      if (count == null || count <= 0) return null;
      return last ? `${fmtNum(count)} predictions through ${last}` : `${fmtNum(count)} predictions`;
    }
    case "publish": {
      // Publishing has its own combined summary -- don't echo step state
      // here; that goes into the detail line below.
      return cascadeSummaryLine(publishCascade, status);
    }
    default:
      return null;
  }
}

/**
 * Human-readable detail under the metric. Priorities:
 *   1. Failed step -> show the error (truncated, full on hover).
 *   2. Running step -> live elapsed + previous-run duration as ETA hint.
 *   3. Completed/skipped step -> finished-at timestamp.
 */
function stepDetail(ctx: StepCtx): ReactNode {
  const { status, info } = ctx;

  if (status === "failed") {
    const msg = info?.error?.trim();
    if (!msg) return null;
    const short = msg.length > 80 ? `${msg.slice(0, 80)}…` : msg;
    return (
      <span className="text-danger-600" title={msg}>
        {short}
      </span>
    );
  }

  if (status === "running") {
    if (!info?.started_at) return "Running…";
    // ``ctx.liveTick`` deliberately read so this re-evaluates each second.
    void ctx.liveTick;
    const elapsed = Math.max(0, (Date.now() - new Date(info.started_at).getTime()) / 1000);
    const eta = info.last_success_duration_seconds;
    return eta && eta > 0
      ? `Running for ${fmtDuration(elapsed)} · last run ${fmtDuration(eta)}`
      : `Running for ${fmtDuration(elapsed)}`;
  }

  if (status === "skipped") {
    return null;
  }

  // completed / idle
  const ts = info?.finished_at ?? info?.started_at;
  return ts ? fmtTimestamp(ts) : null;
}

/**
 * One-line plain-language summary of the cascade for the publish step's
 * metric slot. Uses status priority + the structured cascade payload.
 */
function cascadeSummaryLine(
  cascade: PipelineCascade | null,
  status: StepStatus,
): ReactNode {
  if (status === "idle" || !cascade) {
    return "Pending — runs after a forecast";
  }
  const db = cascade.db_push;
  const refresh = cascade.data_import_refresh;

  if (status === "running") {
    const dbRunning = !db || (!db.success && !db.skipped && !db.error);
    return dbRunning ? "Saving forecast to database…" : "Refreshing downstream apps…";
  }

  if (status === "failed") {
    return db?.error ? "Database save failed" : refresh?.error ? "App refresh failed" : "Publishing failed";
  }

  // completed / skipped
  const dbOk = Boolean(db?.success);
  const refreshOk = Boolean(refresh?.success);
  if (dbOk && refreshOk) return "Database synced · downstream apps refreshed";
  if (dbOk) {
    return refresh?.skipped
      ? "Database synced · downstream refresh not configured"
      : "Database synced";
  }
  if (refreshOk) return "Downstream apps refreshed";
  // Both skipped or both empty.
  if (db?.reason === "db_not_configured") return "Database not configured — skipped";
  return "Nothing to publish";
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
      toast("Training failed to start", "danger");
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
      toast("Forecast generation failed to start", "danger");
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

  // While any pipeline is running, tick once per second so the live
  // elapsed-time line stays current. Stops automatically when nothing is
  // running so the page is idle-quiet.
  const [liveTick, setLiveTick] = useState(0);
  const anyRunning = useMemo(
    () => Object.values(statuses ?? {}).some((s) => s?.status === "running"),
    [statuses],
  );
  useEffect(() => {
    if (!anyRunning) return;
    const id = window.setInterval(() => setLiveTick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, [anyRunning]);

  // Resolve every step once per render. Publishing has its own resolver
  // (worst-of-two-substeps); the rest go through resolveStatus.
  const publish = useMemo(() => resolvePublishStatus(statuses), [statuses]);

  const resolvedSteps = useMemo<ResolvedStep[]>(() => {
    return STEPS.map((step) => {
      const resolution =
        step.key === "publish"
          ? { status: publish.status, info: publish.info }
          : resolveStatus(step, statuses);
      const ctx: StepCtx = {
        step,
        status: resolution.status,
        info: resolution.info,
        publishCascade: publish.cascade,
        summary: summaryData,
        classData,
        liveTick,
      };
      return {
        step,
        status: resolution.status,
        info: resolution.info,
        metric: stepMetric(ctx),
        detail: stepDetail(ctx),
      };
    });
  }, [statuses, summaryData, classData, publish, liveTick]);

  if (statusLoading && summaryLoading) {
    return <Loading message="Loading pipeline..." />;
  }

  const accuracy = summaryData?.accuracy_pct;
  const ov = (summaryData as Record<string, unknown> | undefined)?.training_overview as
    | Record<string, unknown>
    | undefined;
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
  const testRoutes = ov?.test_routes as number | undefined;
  const testItems = ov?.test_items as number | undefined;
  const lastForecastDate = summaryData?.last_forecast_date
    ? fmtDate(summaryData.last_forecast_date)
    : null;

  return (
    <div className="space-y-6">
      <PageHeader
        title="Forecasting Pipeline"
        subtitle="Monitor, control, and understand the demand prediction process."
        actions={<HeaderActions refetchStatus={refetchStatus} />}
      />

      <KpiRow columns={3}>
        <MetricCard
          label="Baseline accuracy"
          value={accuracy != null ? `${accuracy.toFixed(1)}%` : "\u2014"}
          trend={accuracy != null ? (accuracy >= GOOD_SCORE_THRESHOLD ? "up" : "down") : undefined}
          subtitle={`Set at training · ${trainedAgo.replace(/^Trained /, "").replace(/^Not yet trained$/, "not yet trained")}`}
          loading={summaryLoading}
          info={
            <InfoBubble
              title="How forecast accuracy is calculated"
              body={<ForecastAccuracyExplanation />}
            />
          }
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
          subtitle={lastForecastDate ? `Predicting through ${lastForecastDate}` : undefined}
          loading={summaryLoading}
        />
      </KpiRow>

      <Card title="Pipeline flow">
        <PipelineFlow resolved={resolvedSteps} />
      </Card>

      <AutoRetrainSection />
    </div>
  );
}
