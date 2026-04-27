// Route path constants + nav items (client + admin separated)

export const ROUTES = {
  dashboard: "/",
  pipeline: "/pipeline",
  workflow: "/workflow",
  workflowPlan: "/workflow/plan",
  workflowVisit: "/workflow/visit",

  // Admin
  adminData: "/admin/data",
  /** @deprecated Replaced by ROUTES.pipeline — kept for backward-compat redirects. */
  adminPipeline: "/admin/pipeline",
  adminCache: "/admin/cache",
} as const;

export const NAV_ITEMS = [
  { path: ROUTES.dashboard, label: "Dashboard", icon: "grid" },
  { path: ROUTES.pipeline, label: "Forecasting", icon: "trending-up" },
  { path: ROUTES.workflow, label: "Workflow", icon: "clipboard" },
] as const;

// Two-step supervisor flow. Plan = pick a route and review its van load.
// Visit = preview per-customer recommendations, then run the live session.
// Order matters: the stepper renders left-to-right in this order.
export const WORKFLOW_TABS = [
  { path: ROUTES.workflowPlan, label: "Plan", key: "plan", subtitle: "Van load for the route" },
  { path: ROUTES.workflowVisit, label: "Visit", key: "visit", subtitle: "Review recs, run the session" },
] as const;

export const ADMIN_NAV_ITEMS = [
  { path: ROUTES.adminData, label: "Data", icon: "database" },
  { path: ROUTES.adminCache, label: "Cache", icon: "zap" },
] as const;
