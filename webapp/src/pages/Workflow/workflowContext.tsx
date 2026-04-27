import { createContext, useCallback, useContext, useMemo, useState } from "react";
import { todayIso } from "@/lib/date";

/**
 * Cross-step workflow scope. The supervisor picks a (route, date) once
 * and that scope follows them through every step (Plan → Visit). When
 * either is empty, each step falls back to its picker.
 */
interface WorkflowState {
  date: string;
  routeCode: string;
}

interface WorkflowContextValue extends WorkflowState {
  setDate: (v: string) => void;
  setRouteCode: (v: string) => void;
  resetRoute: () => void;
}

function loadInitial(): WorkflowState {
  // Date resets to today on every page load -- a fresh visit should never
  // inherit yesterday's stale date. Route resets too: each session starts
  // at the picker, no surprise pre-fills from a previous browser tab.
  return { date: todayIso(), routeCode: "" };
}

const WorkflowContext = createContext<WorkflowContextValue | null>(null);

export function WorkflowProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<WorkflowState>(loadInitial);

  const setDate = useCallback((date: string) => setState((s) => ({ ...s, date })), []);
  const setRouteCode = useCallback(
    (routeCode: string) => setState((s) => ({ ...s, routeCode })),
    []
  );
  const resetRoute = useCallback(() => setState((s) => ({ ...s, routeCode: "" })), []);

  const value = useMemo(
    () => ({ ...state, setDate, setRouteCode, resetRoute }),
    [state, setDate, setRouteCode, resetRoute]
  );

  return <WorkflowContext.Provider value={value}>{children}</WorkflowContext.Provider>;
}

export function useWorkflow(): WorkflowContextValue {
  const ctx = useContext(WorkflowContext);
  if (!ctx) throw new Error("useWorkflow must be used within WorkflowProvider");
  return ctx;
}
