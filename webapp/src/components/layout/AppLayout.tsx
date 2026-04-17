import { Suspense, useState, useEffect, useCallback } from "react";
import { Outlet } from "react-router-dom";
import Loading from "@/components/ui/Loading";
import CommandPalette from "@/components/ui/CommandPalette";
import Sidebar from "./Sidebar";
import TopBar from "./TopBar";

export default function AppLayout() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [commandOpen, setCommandOpen] = useState(false);

  const openCommandPalette = useCallback(() => setCommandOpen(true), []);

  // Global Cmd+K / Ctrl+K shortcut
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setCommandOpen((prev) => !prev);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, []);

  return (
    <div className="min-h-screen bg-surface-sunken">
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />

      {/* Hamburger button (mobile only) */}
      <button
        onClick={() => setSidebarOpen(true)}
        className="md:hidden fixed top-4 left-4 z-50 p-2 rounded-lg bg-surface-raised shadow-2 border border-default"
        aria-label="Open menu"
      >
        <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M4 6h16M4 12h16M4 18h16" />
        </svg>
      </button>

      <main className="md:ml-52 ml-0 min-h-screen">
        <TopBar onCommandPaletteOpen={openCommandPalette} />
        <div className="p-4 md:p-6 max-w-screen-xl mx-auto">
          <Suspense fallback={<div className="animate-fadeIn"><Loading message="Loading..." /></div>}>
            <div className="animate-fadeIn">
              <Outlet />
            </div>
          </Suspense>
        </div>
      </main>

      <CommandPalette open={commandOpen} onClose={() => setCommandOpen(false)} />
    </div>
  );
}
