import { useEffect } from "react";
import { Link, useLocation } from "react-router-dom";
import { NAV_ITEMS, ROUTES } from "@/config/routes";

const iconPaths: Record<string, string> = {
  grid: "M4 5a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1V5zm10 0a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1V5zM4 15a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1v-4zm10 0a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z",
  "trending-up": "M13 7h8m0 0v8m0-8l-8 8-4-4-6 6",
  clipboard:
    "M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2",
};

function NavIcon({ icon }: { icon: string }) {
  const d = iconPaths[icon] || iconPaths.grid;
  return (
    <svg
      className="h-[18px] w-[18px] shrink-0"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={1.8}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d={d} />
    </svg>
  );
}

export default function Sidebar({ open, onClose }: { open: boolean; onClose: () => void }) {
  const location = useLocation();

  useEffect(() => {
    onClose();
  }, [location.pathname]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    if (open) {
      document.addEventListener("keydown", handleKey);
      return () => document.removeEventListener("keydown", handleKey);
    }
  }, [open, onClose]);

  return (
    <>
      {open && (
        <div
          className="md:hidden fixed inset-0 bg-neutral-900/50 backdrop-blur-sm z-40 transition-opacity duration-base"
          onClick={onClose}
        />
      )}

      <aside
        className={[
          "fixed left-0 top-0 bottom-0 w-52 bg-white border-r border-neutral-200 flex flex-col z-40",
          "transition-transform duration-base md:translate-x-0",
          open ? "translate-x-0" : "-translate-x-full",
        ].join(" ")}
      >
        {/* Brand */}
        <Link
          to={ROUTES.dashboard}
          aria-label="Go to dashboard"
          className="flex items-center gap-2.5 px-4 py-3 shrink-0 group hover:bg-neutral-50 transition-colors"
        >
          <div className="h-7 w-7 rounded-md bg-brand-600 flex items-center justify-center group-hover:bg-brand-500 transition-colors">
            <span className="text-white font-bold text-[10px]">YF</span>
          </div>
          <div className="leading-none">
            <span className="text-label font-semibold text-neutral-800">Yaumi Flow</span>
            <p className="text-[10px] text-neutral-400 mt-0.5">Sales Operations</p>
          </div>
        </Link>

        <div className="mx-3 border-t border-neutral-100" />

        {/* Navigation */}
        <nav className="flex-1 overflow-y-auto py-2 px-2">
          <ul className="flex flex-col gap-1">
            {NAV_ITEMS.map((item) => {
              const isActive =
                item.path === "/"
                  ? location.pathname === "/"
                  : location.pathname.startsWith(item.path);
              return (
                <li key={item.path}>
                  <Link
                    to={item.path}
                    className={[
                      "flex items-center gap-2.5 px-3 py-2.5 rounded-md text-body font-medium transition-all duration-base",
                      isActive
                        ? "bg-brand-50 text-brand-700"
                        : "text-neutral-500 hover:bg-neutral-50 hover:text-neutral-800",
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

        {/* Footer */}
        <div className="px-4 py-2 border-t border-neutral-100">
          <p className="text-[9px] text-neutral-300 uppercase tracking-widest">v1.0</p>
        </div>
      </aside>
    </>
  );
}
