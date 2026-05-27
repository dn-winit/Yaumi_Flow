import { useEffect } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import { ROUTES, WORKFLOW_TABS } from "@/config/routes";
import PageHeader from "@/components/layout/PageHeader";

import { WorkflowProvider, useWorkflow, useWorkflowNavigate } from "./workflowContext";
import VanLoadTab from "./VanLoad/VanLoadTab";
import VisitTab from "./Visit/VisitTab";

/** Two-step Plan -> Visit stepper; (route, date) scope shared via WorkflowContext. */
function WorkflowStepper() {
  const navigate = useWorkflowNavigate();
  const location = useLocation();
  const { routeCode } = useWorkflow();

  // Visit requires a route picked in Plan; stepper, shortcut, and URL guard all enforce this.
  const canEnterVisit = Boolean(routeCode);
  const tabEnabled = (key: string) => key !== "visit" || canEnterVisit;

  // Shortcuts: 1 -> Plan; 2 -> Visit (only when route is picked).
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLElement && ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
      if (e.key === "1") navigate(ROUTES.workflowPlan);
      if (e.key === "2" && canEnterVisit) navigate(ROUTES.workflowVisit);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [navigate, canEnterVisit]);

  const activeIndex = WORKFLOW_TABS.findIndex((t) =>
    location.pathname.startsWith(t.path),
  );
  // Steps left of active are "done"; the row fills in left-to-right.
  const stepStatus = (i: number): "done" | "active" | "idle" => {
    if (i < activeIndex) return "done";
    if (i === activeIndex) return "active";
    return "idle";
  };
  // Connector also lights up when route is set, so the row reads "Plan -> Visit" early.
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
        // Subtitle reflects active scope; avoids a redundant "Workflow for Route X" strip.
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

/** URL guard: Visit requires a picked route; direct hits bounce to Plan. */
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
            {/* Backward-compat: aliases funnel through Plan to preserve ordering. */}
            <Route path="van-load" element={<Navigate to={ROUTES.workflowPlan} replace />} />
            <Route path="orders" element={<Navigate to={ROUTES.workflowPlan} replace />} />
            <Route path="supervision" element={<Navigate to={ROUTES.workflowPlan} replace />} />
          </Routes>
        </div>
      </div>
    </WorkflowProvider>
  );
}
