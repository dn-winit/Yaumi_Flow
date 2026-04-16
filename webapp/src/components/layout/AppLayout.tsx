import React from "react";
import { Outlet } from "react-router-dom";
import Sidebar from "./Sidebar";

export default function AppLayout() {
  return (
    <div className="min-h-screen bg-surface-sunken">
      <Sidebar />
      <main className="ml-60 min-h-screen p-8">
        <Outlet />
      </main>
    </div>
  );
}
