import { useEffect } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { ROUTES, WORKFLOW_TABS } from "@/config/routes";
import PageHeader from "@/components/layout/PageHeader";

import { WorkflowProvider, useWorkflow } from "./workflowContext";
import VanLoadTab from "./VanLoad/VanLoadTab";
import VisitTab from "./Visit/VisitTab";

/**
 * Two-step supervisor flow rendered as a connected stepper, not as
 * independent tabs. Step 1 (Plan) picks the van load for a route; step 2
 * (Visit) reviews per-customer recommendations and runs the live session.
 * The (route, date) scope is shared across both steps via WorkflowContext
 * so a pick made in Plan carries straight into Visit.
 */
function WorkflowStepper() {
  const navigate = useNavigate();
  const location = useLocation();
  const { routeCode } = useWorkflow();

  // Visit is only reachable once the supervisor has picked a route in
  // Plan -- direct jumps would skip the Van Load review step that
  // primes the visit context. The stepper, the keyboard shortcut, and
  // the URL guard in WorkflowPage all enforce the same rule.
  const canEnterVisit = Boolean(routeCode);
  const tabEnabled = (key: string) => key !== "visit" || canEnterVisit;

  // Keyboard shortcuts: 1 jumps to Plan; 2 jumps to Visit only if the
  // route has been picked.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLElement && ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
      if (e.key === "1") navigate(ROUTES.workflowPlan);
      if (e.key === "2" && canEnterVisit) navigate(ROUTES.workflowVisit);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [navigate, canEnterVisit]);

  // Which step is the user currently on.
  const activeIndex = WORKFLOW_TABS.findIndex((t) =>
    location.pathname.startsWith(t.path),
  );
  // Steps are "done" when they are behind the active step. The connector
  // between two steps lights up brand-coloured once the right-hand step
  // has been reached, so the row "fills in" left-to-right as the
  // supervisor advances.
  const stepStatus = (i: number): "done" | "active" | "idle" => {
    if (i < activeIndex) return "done";
    if (i === activeIndex) return "active";
    return "idle";
  };
  // Connector between steps i and i+1 also lights up when the workflow
  // scope (route) is set -- so the row reads "data flows from Plan into
  // Visit" even when the supervisor is still browsing Plan.
  const connectorActive = (i: number): boolean => {
    if (stepStatus(i + 1) !== "idle") return true;
    return Boolean(routeCode);
  };

  return (
    <div className="flex items-center">
      {WORKFLOW_TABS.map((tab, i) => {
        const status = stepStatus(i);
        const enabled = tabEnabled(tab.key);
        const next = WORKFLOW_TABS[i + 1];
        const nextActive = next ? connectorActive(i) : false;
        // Subtitle is dynamic so the stepper itself reflects the active
        // scope -- no need for a separate "Workflow for Route X" strip
        // since the user can read scope directly off the stepper.
        const subtitle = !enabled
          ? "Pick a route in Plan first"
          : routeCode
            ? `Route ${routeCode}`
            : tab.subtitle;
        return (
          <div key={tab.key} className="flex items-center flex-1 last:flex-none">
            <button
              type="button"
              onClick={() => enabled && navigate(tab.path)}
              disabled={!enabled}
              aria-disabled={!enabled}
              title={!enabled ? "Pick a route in Plan first" : undefined}
              className={`group flex items-center gap-3 text-left ${
                enabled ? "" : "cursor-not-allowed opacity-60"
              }`}
            >
              <StepCircle index={i} status={status} />
              <div className="hidden sm:block">
                <p
                  className={`text-body font-semibold leading-tight transition-colors ${
                    status === "active"
                      ? "text-text-primary"
                      : enabled
                        ? "text-text-secondary group-hover:text-text-primary"
                        : "text-text-tertiary"
                  }`}
                >
                  {tab.label}
                  <span className="ml-1.5 text-caption text-text-tertiary font-normal">
                    [{i + 1}]
                  </span>
                </p>
                <p className="text-caption text-text-tertiary leading-snug">
                  {subtitle}
                </p>
              </div>
            </button>
            {next && <Connector active={nextActive} />}
          </div>
        );
      })}
    </div>
  );
}

function StepCircle({
  index,
  status,
}: {
  index: number;
  status: "done" | "active" | "idle";
}) {
  const isDone = status === "done";
  const isActive = status === "active";
  return (
    <div
      className={[
        "relative shrink-0 w-9 h-9 rounded-full flex items-center justify-center text-body font-bold leading-none transition-colors duration-base",
        isDone || isActive
          ? "bg-brand-600 text-white shadow-sm"
          : "bg-surface-sunken text-text-tertiary border-2 border-neutral-200",
      ].join(" ")}
    >
      {isActive && (
        <span className="absolute inset-0 rounded-full bg-brand-600 animate-ping opacity-40" />
      )}
      {isDone ? (
        <svg className="relative w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={3}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
        </svg>
      ) : (
        <span className="relative">{index + 1}</span>
      )}
    </div>
  );
}

function Connector({ active }: { active: boolean }) {
  return (
    <div className="flex-1 h-0.5 mx-3">
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
 * URL guard: Visit is reachable only after a route has been picked in
 * Plan. Direct hits to /workflow/visit (typed URL, stale bookmark, the
 * old /orders or /supervision aliases) bounce back to Plan so the
 * supervisor never lands on an empty session shell.
 */
function VisitGuard() {
  const { routeCode } = useWorkflow();
  if (!routeCode) return <Navigate to={ROUTES.workflowPlan} replace />;
  return <VisitTab />;
}

export default function WorkflowPage() {
  return (
    <WorkflowProvider>
      <div className="space-y-0">
        <div className="px-6 pt-6">
          <PageHeader
            title="Workflow"
            subtitle="Plan the van, then visit the customers."
          />
        </div>
        <div className="px-6 pb-4 border-b border-default bg-surface-raised">
          <WorkflowStepper />
        </div>
        <div className="p-6">
          <Routes>
            <Route index element={<Navigate to={ROUTES.workflowPlan} replace />} />
            <Route path="plan" element={<VanLoadTab />} />
            <Route path="visit" element={<VisitGuard />} />
            {/* Backward-compat: every alias funnels through Plan so the
                Plan -> Van Load -> Visit ordering is preserved. */}
            <Route path="van-load" element={<Navigate to={ROUTES.workflowPlan} replace />} />
            <Route path="orders" element={<Navigate to={ROUTES.workflowPlan} replace />} />
            <Route path="supervision" element={<Navigate to={ROUTES.workflowPlan} replace />} />
          </Routes>
        </div>
      </div>
    </WorkflowProvider>
  );
}
