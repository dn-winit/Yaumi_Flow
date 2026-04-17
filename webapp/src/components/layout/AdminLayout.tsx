import { Suspense, useState, useEffect } from "react";
import { Link, Outlet, useLocation } from "react-router-dom";
import Loading from "@/components/ui/Loading";
import { ADMIN_NAV_ITEMS, ROUTES } from "@/config/routes";

const iconPaths: Record<string, string> = {
  database:
    "M4 7c0-1.657 3.582-3 8-3s8 1.343 8 3-3.582 3-8 3-8-1.343-8-3zm0 0v10c0 1.657 3.582 3 8 3s8-1.343 8-3V7M4 12c0 1.657 3.582 3 8 3s8-1.343 8-3",
  cpu: "M9 3v2m6-2v2M9 19v2m6-2v2M5 9H3m2 6H3m18-6h-2m2 6h-2M7 19h10a2 2 0 002-2V7a2 2 0 00-2-2H7a2 2 0 00-2 2v10a2 2 0 002 2zM9 9h6v6H9V9z",
  zap: "M13 10V3L4 14h7v7l9-11h-7z",
  home: "M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-4 0h4",
};

function NavIcon({ icon }: { icon: string }) {
  const d = iconPaths[icon] ?? iconPaths.cpu;
  return (
    <svg
      className="h-5 w-5 shrink-0"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={1.5}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d={d} />
    </svg>
  );
}

export default function AdminLayout() {
  const location = useLocation();
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // Close sidebar on route change (mobile)
  useEffect(() => {
    setSidebarOpen(false);
  }, [location.pathname]);

  // Close on Escape key (mobile)
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setSidebarOpen(false);
    };
    if (sidebarOpen) {
      document.addEventListener("keydown", handleKey);
      return () => document.removeEventListener("keydown", handleKey);
    }
  }, [sidebarOpen]);

  return (
    <div className="min-h-screen bg-surface-sunken">
      {/* Backdrop (mobile only) */}
      {sidebarOpen && (
        <div
          className="md:hidden fixed inset-0 bg-neutral-900/40 z-40 transition-opacity duration-base"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <aside
        className={[
          "fixed left-0 top-0 bottom-0 w-60 bg-neutral-900 text-neutral-100 flex flex-col z-40",
          "transition-transform duration-base md:translate-x-0",
          sidebarOpen ? "translate-x-0" : "-translate-x-full",
        ].join(" ")}
      >
        {/* Brand */}
        <div className="flex items-center gap-2 px-6 h-16 border-b border-neutral-800 shrink-0">
          <div className="h-8 w-8 rounded-lg bg-warning-500 flex items-center justify-center">
            <span className="text-neutral-900 font-bold text-body">YF</span>
          </div>
          <span className="text-lg font-bold">Yaumi Admin</span>
        </div>

        {/* Navigation */}
        <nav className="flex-1 overflow-y-auto py-4 px-3">
          <ul className="flex flex-col gap-1">
            {ADMIN_NAV_ITEMS.map((item) => {
              const isActive = location.pathname === item.path;
              return (
                <li key={item.path}>
                  <Link
                    to={item.path}
                    className={[
                      "flex items-center gap-3 px-3 py-2.5 rounded-lg text-body font-medium transition-colors",
                      isActive
                        ? "bg-warning-500/10 text-warning-500"
                        : "text-text-tertiary hover:bg-neutral-800 hover:text-neutral-100",
                    ].join(" ")}
                  >
                    <NavIcon icon={item.icon} />
                    {item.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>

        {/* Back to main app */}
        <div className="border-t border-neutral-800 p-3">
          <Link
            to={ROUTES.dashboard}
            className="flex items-center gap-3 px-3 py-2.5 rounded-lg text-body font-medium text-text-tertiary hover:bg-neutral-800 hover:text-neutral-100 transition-colors"
          >
            <NavIcon icon="home" />
            Back to App
          </Link>
        </div>
      </aside>

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

      <main className="md:ml-60 ml-0 min-h-screen">
        <header className="bg-surface-raised border-b border-default px-4 md:px-8 py-4">
          <h2 className="text-body font-semibold uppercase tracking-wider text-warning-600">
            Admin
          </h2>
        </header>
        <div className="p-4 md:p-8">
          <Suspense fallback={<div className="animate-fadeIn"><Loading message="Loading..." /></div>}>
            <Outlet />
          </Suspense>
        </div>
      </main>
    </div>
  );
}
