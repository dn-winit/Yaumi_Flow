import { Routes, Route, Navigate } from "react-router-dom";
import AppLayout from "@/components/layout/AppLayout";
import AdminLayout from "@/components/layout/AdminLayout";
import { ROUTES } from "@/config/routes";
import DashboardPage from "@/pages/Dashboard/DashboardPage";
import PipelinePage from "@/pages/Pipeline/PipelinePage";
import WorkflowPage from "@/pages/Workflow/WorkflowPage";
import DataAdminPage from "@/pages/Admin/DataAdminPage";
import CacheAdminPage from "@/pages/Admin/CacheAdminPage";

export default function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route path={ROUTES.dashboard} element={<DashboardPage />} />
        <Route path={ROUTES.pipeline} element={<PipelinePage />} />
        <Route path={`${ROUTES.workflow}/*`} element={<WorkflowPage />} />
        <Route path="*" element={<Navigate to={ROUTES.dashboard} replace />} />
      </Route>
      <Route element={<AdminLayout />}>
        <Route path={ROUTES.adminData} element={<DataAdminPage />} />
        <Route path={ROUTES.adminPipeline} element={<Navigate to={ROUTES.pipeline} replace />} />
        <Route path={ROUTES.adminCache} element={<CacheAdminPage />} />
      </Route>
    </Routes>
  );
}
