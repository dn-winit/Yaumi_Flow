import React, { useEffect } from "react";
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { ROUTES, WORKFLOW_TABS } from "@/config/routes";
import PageHeader from "@/components/layout/PageHeader";

import { WorkflowProvider } from "./workflowContext";
import VanLoadTab from "./VanLoad/VanLoadTab";
import OrdersTab from "./Orders/OrdersTab";
import SupervisionTab from "./Supervision/SupervisionTab";

function WorkflowTabs() {
  const navigate = useNavigate();
  const location = useLocation();

  // Keyboard shortcuts: 1/2/3 switch tabs
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLElement && ["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
      if (e.key === "1") navigate(ROUTES.workflowVanLoad);
      if (e.key === "2") navigate(ROUTES.workflowOrders);
      if (e.key === "3") navigate(ROUTES.workflowSupervision);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [navigate]);

  return (
    <div className="border-b border-default bg-surface-raised">
      <div className="flex items-center px-6">
        {WORKFLOW_TABS.map((tab, i) => {
          const active = location.pathname.startsWith(tab.path);
          const isLast = i === WORKFLOW_TABS.length - 1;
          return (
            <React.Fragment key={tab.key}>
              <NavLink
                to={tab.path}
                className={`px-4 py-3 text-body font-medium border-b-2 transition-all duration-base ${
                  active
                    ? "border-brand-600 text-brand-700"
                    : "border-transparent text-text-secondary hover:text-text-primary hover:border-strong"
                }`}
              >
                {tab.label}
                <span className="ml-2 text-caption text-text-tertiary">[{i + 1}]</span>
              </NavLink>
              {!isLast && (
                <span className="text-text-tertiary shrink-0 mx-1 text-body" aria-hidden>→</span>
              )}
            </React.Fragment>
          );
        })}
      </div>
    </div>
  );
}

export default function WorkflowPage() {
  return (
    <WorkflowProvider>
      <div className="space-y-0">
        <div className="px-6 pt-6">
          <PageHeader
            title="Workflow"
            subtitle="Plan the van, review recommendations, and supervise execution."
          />
        </div>
        <WorkflowTabs />
        <div className="p-6">
          <Routes>
            <Route index element={<Navigate to={ROUTES.workflowVanLoad} replace />} />
            <Route path="van-load" element={<VanLoadTab />} />
            <Route path="orders" element={<OrdersTab />} />
            <Route path="supervision" element={<SupervisionTab />} />
          </Routes>
        </div>
      </div>
    </WorkflowProvider>
  );
}
