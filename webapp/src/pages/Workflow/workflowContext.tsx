import { createContext, useCallback, useContext, useMemo } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { todayIso } from "@/lib/date";

/**
 * Cross-step workflow scope. The supervisor picks a (route, date) once
 * and that scope follows them through every step (Plan -> Visit). The
 * scope is mirrored in the URL query string (``?route=…&date=…``) so a
 * page refresh, a back/forward navigation, or a copy-pasted link all
 * land the user back where they were instead of the route picker.
 *
 * Date defaults to today when the URL has no ``date`` param so a clean
 * URL still works. Route defaults to empty so each step falls back to
 * its picker until the supervisor selects one.
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

const _PARAM_ROUTE = "route";
const _PARAM_DATE = "date";

const WorkflowContext = createContext<WorkflowContextValue | null>(null);

export function WorkflowProvider({ children }: { children: React.ReactNode }) {
  const [params, setParams] = useSearchParams();

  // Read state directly from the URL on every render -- single source of
  // truth, no parallel React state to drift out of sync. Browser
  // refresh, back/forward, and direct URL paste all yield the same
  // scope because the URL is the scope.
  const date = params.get(_PARAM_DATE) || todayIso();
  const routeCode = params.get(_PARAM_ROUTE) || "";

  // ``replace: true`` so each pick / date change overwrites the current
  // history entry instead of stacking; the back button still walks
  // sibling pages, not micro-steps inside the workflow.
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

/**
 * Workflow-aware navigation: jump to a sibling step (Plan / Visit) while
 * preserving the ``?route=…&date=…`` query string. Without this, every
 * cross-step navigation strips the scope and the next page falls back
 * to its picker.
 */
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
