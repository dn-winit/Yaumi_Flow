import { Routes, Route, Navigate } from "react-router-dom";
import AppLayout from "@/components/layout/AppLayout";
import AdminLayout from "@/components/layout/AdminLayout";
import { ROUTES } from "@/config/routes";
import DashboardPage from "@/pages/Dashboard/DashboardPage";
import WorkflowPage from "@/pages/Workflow/WorkflowPage";
import DataAdminPage from "@/pages/Admin/DataAdminPage";
import PipelineAdminPage from "@/pages/Admin/PipelineAdminPage";
import CacheAdminPage from "@/pages/Admin/CacheAdminPage";

export default function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route path={ROUTES.dashboard} element={<DashboardPage />} />
        <Route path={`${ROUTES.workflow}/*`} element={<WorkflowPage />} />
        <Route path="*" element={<Navigate to={ROUTES.dashboard} replace />} />
      </Route>
      <Route element={<AdminLayout />}>
        <Route path={ROUTES.adminData} element={<DataAdminPage />} />
        <Route path={ROUTES.adminPipeline} element={<PipelineAdminPage />} />
        <Route path={ROUTES.adminCache} element={<CacheAdminPage />} />
      </Route>
    </Routes>
  );
}
