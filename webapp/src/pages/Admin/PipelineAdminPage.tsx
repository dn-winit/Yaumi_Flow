import { usePipelineStatus, useTriggerPipeline } from "@/hooks/useForecast";
import Loading from "@/components/ui/Loading";
import Card from "@/components/ui/Card";
import Button from "@/components/ui/Button";
import Badge from "@/components/ui/Badge";
import EmptyState from "@/components/ui/EmptyState";
import PageHeader from "@/components/layout/PageHeader";

type StatusVariant = "success" | "warning" | "danger" | "neutral";

function statusVariant(status: string): StatusVariant {
  if (status === "completed" || status === "success") return "success";
  if (status === "running" || status === "pending") return "warning";
  if (status === "failed" || status === "error") return "danger";
  return "neutral";
}

export default function PipelineAdminPage() {
  const { data: statuses, loading, refetch } = usePipelineStatus();
  const {
    triggerTrain,
    triggerInference,
    loading: triggerLoading,
    error,
  } = useTriggerPipeline();

  const handleTrain = async () => {
    try {
      await triggerTrain();
      refetch();
    } catch {
      // error managed by hook
    }
  };

  const handleInference = async () => {
    try {
      await triggerInference();
      refetch();
    } catch {
      // error managed by hook
    }
  };

  if (loading) {
    return <Loading message="Loading pipeline status..." />;
  }

  return (
    <div className="space-y-6">
      <PageHeader title="Pipeline" />

      <Card title="Pipeline Actions">
        <div className="flex items-center gap-3">
          <Button
            variant="primary"
            loading={triggerLoading}
            onClick={handleTrain}
          >
            Train Models
          </Button>
          <Button
            variant="secondary"
            loading={triggerLoading}
            onClick={handleInference}
          >
            Run Inference
          </Button>
          <Button variant="ghost" onClick={() => refetch()}>
            Refresh Status
          </Button>
        </div>
        {error && <p className="text-body text-danger-600 mt-3">{error}</p>}
      </Card>

      <Card title="Pipeline Status">
        {statuses && Object.keys(statuses).length > 0 ? (
          <div className="space-y-4">
            {Object.entries(statuses).map(([name, info]) => (
              <div
                key={name}
                className="flex items-center justify-between border-b border-subtle pb-3 last:border-0 last:pb-0"
              >
                <div>
                  <p className="text-body font-semibold text-text-primary">{name}</p>
                  {info.started_at && (
                    <p className="text-caption text-text-tertiary">
                      Started: {info.started_at}
                    </p>
                  )}
                  {info.duration_seconds > 0 && (
                    <p className="text-caption text-text-tertiary">
                      Duration: {info.duration_seconds.toFixed(1)}s
                    </p>
                  )}
                  {info.error && (
                    <p className="text-caption text-danger-600 mt-1">{info.error}</p>
                  )}
                </div>
                <Badge variant={statusVariant(info.status)}>{info.status}</Badge>
              </div>
            ))}
          </div>
        ) : (
          <EmptyState title="No pipeline status available" icon="⚙️" />
        )}
      </Card>
    </div>
  );
}
