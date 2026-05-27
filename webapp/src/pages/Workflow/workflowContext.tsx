import { createContext, useCallback, useContext, useMemo } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { todayIso } from "@/lib/date";

/** Cross-step (route, date) scope mirrored in `?route=...&date=...` so refresh/back/paste all work.
 *  Date defaults to today; route defaults to empty so each step falls back to its picker. */
interface WorkflowState {
  date: string;
  routeCode: string;
}

interface WorkflowContextValue extends WorkflowState {
  setDate: (v: string) => void;
  setRouteCode: (v: string) => void;
  resetRoute: () => void;
}

const _PARAM_ROUTE = "route";
const _PARAM_DATE = "date";

const WorkflowContext = createContext<WorkflowContextValue | null>(null);

export function WorkflowProvider({ children }: { children: React.ReactNode }) {
  const [params, setParams] = useSearchParams();

  // URL is the single source of truth (no parallel React state).
  const date = params.get(_PARAM_DATE) || todayIso();
  const routeCode = params.get(_PARAM_ROUTE) || "";

  // replace:true so picks don't stack history entries.
  const update = useCallback(
    (mutator: (next: URLSearchParams) => void) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          mutator(next);
          return next;
        },
        { replace: true },
      );
    },
    [setParams],
  );

  const setDate = useCallback(
    (v: string) => update((p) => (v ? p.set(_PARAM_DATE, v) : p.delete(_PARAM_DATE))),
    [update],
  );
  const setRouteCode = useCallback(
    (v: string) => update((p) => (v ? p.set(_PARAM_ROUTE, v) : p.delete(_PARAM_ROUTE))),
    [update],
  );
  const resetRoute = useCallback(
    () => update((p) => p.delete(_PARAM_ROUTE)),
    [update],
  );

  const value = useMemo(
    () => ({ date, routeCode, setDate, setRouteCode, resetRoute }),
    [date, routeCode, setDate, setRouteCode, resetRoute],
  );

  return <WorkflowContext.Provider value={value}>{children}</WorkflowContext.Provider>;
}

export function useWorkflow(): WorkflowContextValue {
  const ctx = useContext(WorkflowContext);
  if (!ctx) throw new Error("useWorkflow must be used within WorkflowProvider");
  return ctx;
}

/** Cross-step navigation that preserves the `?route=...&date=...` scope. */
export function useWorkflowNavigate() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  return useCallback(
    (path: string) => {
      const qs = params.toString();
      navigate(qs ? `${path}?${qs}` : path);
    },
    [navigate, params],
  );
}
