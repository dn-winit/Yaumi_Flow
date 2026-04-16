import { Link, useLocation } from "react-router-dom";
import { NAV_ITEMS, ROUTES } from "@/config/routes";

const iconPaths: Record<string, string> = {
  grid: "M4 5a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1V5zm10 0a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1V5zM4 15a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1H5a1 1 0 01-1-1v-4zm10 0a1 1 0 011-1h4a1 1 0 011 1v4a1 1 0 01-1 1h-4a1 1 0 01-1-1v-4z",
  database:
    "M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-4 0h4",
  "trending-up":
    "M13 7h8m0 0v8m0-8l-8 8-4-4-6 6",
  clipboard:
    "M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2",
  "map-pin":
    "M17.657 16.657L13.414 20.9a2 2 0 01-2.828 0l-4.243-4.243a8 8 0 1111.314 0z M15 11a3 3 0 11-6 0 3 3 0 016 0z",
  brain:
    "M9.663 17h4.673M12 3v1m6.364 1.636l-.707.707M21 12h-1M4 12H3m3.343-5.657l-.707-.707m2.828 9.9a5 5 0 117.072 0l-.548.547A3.374 3.374 0 0014 18.469V19a2 2 0 11-4 0v-.531c0-.895-.356-1.754-.988-2.386l-.548-.547z",
  "bar-chart":
    "M3 3v18h18 M7 17V10 M12 17V6 M17 17v-4",
};

function NavIcon({ icon }: { icon: string }) {
  const d = iconPaths[icon] || iconPaths.grid;
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

export default function Sidebar() {
  const location = useLocation();

  return (
    <aside className="fixed left-0 top-0 bottom-0 w-60 bg-surface-raised border-r border-default flex flex-col z-40">
      {/* Brand */}
      <Link
        to={ROUTES.dashboard}
        aria-label="Go to dashboard"
        className="flex items-center gap-2 px-6 h-16 border-b border-default shrink-0 hover:bg-surface-sunken transition-colors"
      >
        <img
          src="/yaumi-logo.png"
          alt="Yaumi"
          className="h-8 w-auto shrink-0 transition-transform hover:scale-105"
        />
        <div className="flex flex-col leading-tight">
          <span className="text-base font-bold text-text-primary">Yaumi Flow</span>
          <p className="text-caption text-text-tertiary -mt-0.5">Sales Operations</p>
        </div>
      </Link>

      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto py-4 px-3">
        <ul className="flex flex-col gap-1">
          {NAV_ITEMS.map((item) => {
            const isActive = location.pathname === item.path;
            return (
              <li key={item.path}>
                <Link
                  to={item.path}
                  className={[
                    "flex items-center gap-3 px-3 py-2.5 rounded-lg text-sm font-medium transition-colors",
                    isActive
                      ? "bg-brand-50 text-brand-700"
                      : "text-text-secondary hover:bg-surface-sunken hover:text-text-primary",
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
    </aside>
  );
}
