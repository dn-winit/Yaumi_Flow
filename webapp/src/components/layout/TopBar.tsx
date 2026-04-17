import { Link, useLocation } from "react-router-dom";

const PATH_LABELS: Record<string, string> = {
  "": "Dashboard",
  pipeline: "Forecasting",
  workflow: "Workflow",
  "workflow/van-load": "Van Load",
  "workflow/orders": "Recommended Orders",
  "workflow/supervision": "Supervision",
  admin: "Admin",
  "admin/data": "Data",
  "admin/cache": "Cache",
};

interface TopBarProps {
  onCommandPaletteOpen: () => void;
}

export default function TopBar({ onCommandPaletteOpen }: TopBarProps) {
  const location = useLocation();

  // Build breadcrumbs from the current pathname
  const segments = location.pathname.replace(/^\//, "").split("/").filter(Boolean);
  const crumbs: { label: string; path: string }[] = [
    { label: "Dashboard", path: "/" },
  ];

  if (segments.length > 0) {
    let cumulative = "";
    for (const seg of segments) {
      cumulative = cumulative ? `${cumulative}/${seg}` : seg;
      const label = PATH_LABELS[cumulative] ?? seg;
      crumbs.push({ label, path: `/${cumulative}` });
    }
  }

  // If we're on "/", just show "Dashboard" as a single non-linked crumb
  const isHome = location.pathname === "/";

  return (
    <div className="sticky top-0 z-30 bg-white border-b border-neutral-100 px-6 h-12 flex items-center">
      {/* Breadcrumbs */}
      <nav className="flex items-center gap-1 text-caption">
        {isHome ? (
          <span className="text-neutral-800 font-medium">Dashboard</span>
        ) : (
          crumbs.map((crumb, i) => {
            const isLast = i === crumbs.length - 1;
            return (
              <span key={crumb.path} className="flex items-center gap-1">
                {i > 0 && <span className="text-neutral-300">/</span>}
                {isLast ? (
                  <span className="text-neutral-800 font-medium">
                    {crumb.label}
                  </span>
                ) : (
                  <Link
                    to={crumb.path}
                    className="text-neutral-500 hover:text-neutral-700 transition-colors"
                  >
                    {crumb.label}
                  </Link>
                )}
              </span>
            );
          })
        )}
      </nav>

      {/* Spacer */}
      <div className="flex-1" />

      {/* Cmd+K hint */}
      <button
        onClick={onCommandPaletteOpen}
        className="flex items-center gap-1 px-2 py-1 rounded-md border border-neutral-200 bg-neutral-50 text-caption text-neutral-500 hover:bg-neutral-100 transition-colors mr-3"
      >
        <kbd className="font-sans">⌘K</kbd>
      </button>

      {/* Notification bell placeholder */}
      <button aria-label="Notifications" className="relative p-1.5 rounded-md text-neutral-400 hover:text-neutral-600 hover:bg-neutral-50 transition-colors">
        <svg
          className="h-4.5 w-4.5"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={1.5}
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M14.857 17.082a23.848 23.848 0 005.454-1.31A8.967 8.967 0 0118 9.75V9A6 6 0 006 9v.75a8.967 8.967 0 01-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a24.255 24.255 0 01-5.714 0m5.714 0a3 3 0 11-5.714 0"
          />
        </svg>
        <span className="absolute -top-0.5 -right-0.5 flex items-center justify-center h-3.5 w-3.5 rounded-full bg-neutral-200 text-[9px] font-medium text-neutral-500">
          0
        </span>
      </button>
    </div>
  );
}
