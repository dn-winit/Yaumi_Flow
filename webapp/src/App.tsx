import React, { Suspense } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import AppLayout from "@/components/layout/AppLayout";
import AdminLayout from "@/components/layout/AdminLayout";
import ErrorBoundary from "@/components/ErrorBoundary";
import Skeleton from "@/components/ui/Skeleton";
import { ROUTES } from "@/config/routes";

const DashboardPage = React.lazy(() => import("@/pages/Dashboard/DashboardPage"));
const WorkflowPage = React.lazy(() => import("@/pages/Workflow/WorkflowPage"));
const PipelinePage = React.lazy(() => import("@/pages/Pipeline/PipelinePage"));
const DataAdminPage = React.lazy(() => import("@/pages/Admin/DataAdminPage"));
const CacheAdminPage = React.lazy(() => import("@/pages/Admin/CacheAdminPage"));

/**
 * Wrap every routed page with a Suspense fallback AND an ErrorBoundary so a
 * lazy-chunk failure or a render-time throw on one page never white-screens
 * the whole app. Scope label flows into the boundary so console traces
 * identify the offending surface immediately.
 */
function RouteShell({ scope, children }: { scope: string; children: React.ReactNode }) {
  return (
    <ErrorBoundary scope={scope}>
      <Suspense fallback={<Skeleton className="m-6 h-48 w-full max-w-3xl" />}>{children}</Suspense>
    </ErrorBoundary>
  );
}

export default function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route
          path={ROUTES.dashboard}
          element={
            <RouteShell scope="dashboard">
              <DashboardPage />
            </RouteShell>
          }
        />
        <Route
          path={ROUTES.pipeline}
          element={
            <RouteShell scope="pipeline">
              <PipelinePage />
            </RouteShell>
          }
        />
        <Route
          path={`${ROUTES.workflow}/*`}
          element={
            <RouteShell scope="workflow">
              <WorkflowPage />
            </RouteShell>
          }
        />
        <Route path="*" element={<Navigate to={ROUTES.dashboard} replace />} />
      </Route>
      <Route element={<AdminLayout />}>
        <Route
          path={ROUTES.adminData}
          element={
            <RouteShell scope="admin-data">
              <DataAdminPage />
            </RouteShell>
          }
        />
        <Route path={ROUTES.adminPipeline} element={<Navigate to={ROUTES.pipeline} replace />} />
        <Route
          path={ROUTES.adminCache}
          element={
            <RouteShell scope="admin-cache">
              <CacheAdminPage />
            </RouteShell>
          }
        />
      </Route>
    </Routes>
  );
}
