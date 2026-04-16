// Route path constants + nav items (client + admin separated)

export const ROUTES = {
  dashboard: "/",
  pipeline: "/pipeline",
  workflow: "/workflow",
  workflowVanLoad: "/workflow/van-load",
  workflowOrders: "/workflow/orders",
  workflowSupervision: "/workflow/supervision",

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

export const WORKFLOW_TABS = [
  { path: ROUTES.workflowVanLoad, label: "Van Load", key: "van-load" },
  { path: ROUTES.workflowOrders, label: "Recommended Orders", key: "orders" },
  { path: ROUTES.workflowSupervision, label: "Supervision", key: "supervision" },
] as const;

export const ADMIN_NAV_ITEMS = [
  { path: ROUTES.adminData, label: "Data", icon: "database" },
  { path: ROUTES.adminCache, label: "Cache", icon: "zap" },
] as const;
