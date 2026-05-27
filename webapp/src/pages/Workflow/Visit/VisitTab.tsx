import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Card from "@/components/ui/Card";
import Button from "@/components/ui/Button";
import EmptyState from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { ROUTES } from "@/config/routes";
import { useRecommendations, useGenerate } from "@/hooks/useRecommendedOrder";
import { useToast } from "@/hooks/useToast";
import { VISIT_REC_LIMIT } from "@/lib/format";
import { fmtDate } from "@/lib/date";
import { useWorkflow, useWorkflowNavigate } from "@/pages/Workflow/workflowContext";
import LiveSessionTab from "@/pages/Supervision/LiveSession/LiveSessionTab";
import { supervisionApi } from "@/api/supervision";
import type { SessionSummary } from "@/types/supervision";
import AdoptionDrawer from "./AdoptionDrawer";
import UpcomingPlanDrawer from "./UpcomingPlanDrawer";

/** Visit step; entered with route+date guaranteed (enforced by VisitGuard in WorkflowPage). */
export default function VisitTab() {
  const navigate = useWorkflowNavigate();
  const { date, routeCode, resetRoute } = useWorkflow();

  // Picking a different route means returning to Plan; the workflow
  // does not expose its own route picker on the Visit step.
  const onPickAnotherRoute = useCallback(() => {
    resetRoute();
    navigate(ROUTES.workflowPlan);
  }, [navigate, resetRoute]);

  return (
    <VisitSession
      routeCode={routeCode}
      date={date}
      onPickAnotherRoute={onPickAnotherRoute}
    />
  );
}

// Session shell: fetch recs, auto-init session, render LiveSessionTab with drawer triggers.
function VisitSession({
  routeCode,
  date,
  onPickAnotherRoute,
}: {
  routeCode: string;
  date: string;
  onPickAnotherRoute: () => void;
}) {
  const navigate = useWorkflowNavigate();
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [sessionData, setSessionData] = useState<SessionSummary | null>(null);
  const [initError, setInitError] = useState<string | null>(null);
  const [adoptionOpen, setAdoptionOpen] = useState(false);
  const [upcomingOpen, setUpcomingOpen] = useState(false);
  const { toast } = useToast();

  // Pull the per-customer recs for the route + date.
  const recsParams = useMemo(
    () => ({ date, route_code: routeCode, limit: VISIT_REC_LIMIT, offset: 0 }),
    [date, routeCode],
  );
  const recs = useRecommendations(recsParams);
  const { execute: generate, loading: generating } = useGenerate();

  const recordings = recs.data?.data ?? [];

  // Auto-init runs in parallel with recs (server fetches its own recs); initRef makes it idempotent.
  const initRef = useRef<{ key: string; promise: Promise<void> } | null>(null);
  useEffect(() => {
    if (sessionId) return;
    if (!routeCode || !date) return;

    const key = `${routeCode}|${date}`;
    if (initRef.current?.key === key) return;

    setInitError(null);
    const promise = (async () => {
      try {
        const sessionRes = await supervisionApi.initSession(routeCode, date);
        const session = sessionRes.session;
        setSessionId(session.sessionId);
        setSessionData(session);
      } catch (err) {
        setInitError(err instanceof Error ? err.message : "Failed to initialize session");
      }
    })();
    initRef.current = { key, promise };
  }, [sessionId, routeCode, date]);

  const handleGenerate = async () => {
    try {
      await generate(date, [routeCode], true);
      recs.refetch();
      toast("Suggestions built", "success");
    } catch (err) {
      toast(err instanceof Error ? err.message : "Generation failed", "danger");
    }
  };

  // Drawer triggers + back-to-Plan handed to LiveSessionTab for the same ContextStrip row.
  const sessionExtraActions = (
    <>
      <Button variant="ghost" size="sm" onClick={() => navigate(ROUTES.workflowPlan)}>
        ← Back to Plan
      </Button>
      <Button variant="secondary" size="sm" onClick={() => setAdoptionOpen(true)}>
        Past performance
      </Button>
      <Button variant="secondary" size="sm" onClick={() => setUpcomingOpen(true)}>
        Upcoming plan
      </Button>
    </>
  );

  // ----- States before the session is live -----

  if (recs.loading) {
    // Layout-shaped skeleton; same height as LiveSessionTab to avoid jump on swap.
    return (
      <div className="space-y-4">
        <Skeleton className="h-12" />
        <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-4">
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <Skeleton key={i} className="h-28" />
          ))}
        </div>
      </div>
    );
  }

  if (recs.error) {
    return (
      <Card>
        <EmptyState
          icon="⚠️"
          title="Could not load recommendations"
          message={recs.error}
        />
      </Card>
    );
  }

  if (recordings.length === 0) {
    const diag = recs.data?.diagnosis;
    return (
      <Card>
        <EmptyState
          icon={diag ? "🔔" : "📦"}
          title={diag?.headline ?? "No suggestions yet"}
          message={
            diag?.detail ??
            `No suggestions ready for route ${routeCode} on ${fmtDate(date)}. They run automatically each night — or build them now.`
          }
          action={
            <Button
              variant={diag ? "ghost" : "primary"}
              size="sm"
              loading={generating}
              onClick={handleGenerate}
            >
              {diag ? "Try again" : `Build now for route ${routeCode}`}
            </Button>
          }
        />
      </Card>
    );
  }

  if (initError) {
    return (
      <Card>
        <EmptyState icon="⚠️" title="Could not start the session" message={initError} />
      </Card>
    );
  }

  if (!sessionId) {
    // Brief gap between recs landing and /initialize completing.
    return (
      <div className="space-y-4">
        <Skeleton className="h-12" />
        <Skeleton className="h-48" />
      </div>
    );
  }

  // ----- Live session -----

  return (
    <div className="animate-fade-in">
      <LiveSessionTab
        sessionId={sessionId}
        sessionData={sessionData}
        routeCode={routeCode}
        date={date}
        extraActions={sessionExtraActions}
        onPickAnotherRoute={onPickAnotherRoute}
      />
      <AdoptionDrawer
        open={adoptionOpen}
        onClose={() => setAdoptionOpen(false)}
        routeCode={routeCode}
      />
      <UpcomingPlanDrawer
        open={upcomingOpen}
        onClose={() => setUpcomingOpen(false)}
        routeCode={routeCode}
      />
    </div>
  );
}
