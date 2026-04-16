import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { todayIso } from "@/lib/date";

/**
 * Cross-tab workflow state. ``routeCode`` is deliberately NOT here -- each tab
 * (Van Load, Orders) owns its own route selection so switching tabs doesn't
 * carry a route over and skip the route-picker grid.
 */
interface WorkflowState {
  date: string;
  selectedItems: string[];
}

interface WorkflowContextValue extends WorkflowState {
  setDate: (v: string) => void;
  setSelectedItems: (v: string[]) => void;
  reset: () => void;
}

const STORAGE_KEY = "yaumi.workflow.state";

function loadInitial(): WorkflowState {
  // Date always resets to "today" on page load so a fresh visit never shows
  // yesterday's stale date from a previous session. Only non-date state is
  // restored from localStorage.
  const today = todayIso();
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) {
      const parsed = JSON.parse(raw);
      return {
        date: today,
        selectedItems: Array.isArray(parsed.selectedItems) ? parsed.selectedItems : [],
      };
    }
  } catch {
    // ignore
  }
  return { date: today, selectedItems: [] };
}

const WorkflowContext = createContext<WorkflowContextValue | null>(null);

export function WorkflowProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<WorkflowState>(loadInitial);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch {
      // ignore quota
    }
  }, [state]);

  const setDate = useCallback((date: string) => setState((s) => ({ ...s, date })), []);
  const setSelectedItems = useCallback(
    (selectedItems: string[]) => setState((s) => ({ ...s, selectedItems })),
    []
  );
  const reset = useCallback(
    () => setState({ date: todayIso(), selectedItems: [] }),
    []
  );

  const value = useMemo(
    () => ({ ...state, setDate, setSelectedItems, reset }),
    [state, setDate, setSelectedItems, reset]
  );

  return <WorkflowContext.Provider value={value}>{children}</WorkflowContext.Provider>;
}

export function useWorkflow(): WorkflowContextValue {
  const ctx = useContext(WorkflowContext);
  if (!ctx) throw new Error("useWorkflow must be used within WorkflowProvider");
  return ctx;
}
