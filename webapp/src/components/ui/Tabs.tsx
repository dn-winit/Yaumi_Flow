interface Tab {
  key: string;
  label: string;
}

interface TabsProps {
  tabs: Tab[];
  activeTab: string;
  onTabChange: (key: string) => void;
}

export default function Tabs({ tabs, activeTab, onTabChange }: TabsProps) {
  return (
    <div className="border-b border-default">
      <nav className="flex gap-6 -mb-px" role="tablist" aria-label="Tabs">
        {tabs.map((tab) => {
          const isActive = tab.key === activeTab;
          return (
            <button
              key={tab.key}
              role="tab"
              aria-selected={isActive}
              onClick={() => onTabChange(tab.key)}
              className={[
                "py-3 text-body font-medium border-b-2 transition-all duration-base whitespace-nowrap",
                isActive
                  ? "border-brand-600 text-brand-600"
                  : "border-transparent text-text-secondary hover:text-text-primary hover:border-strong",
              ].join(" ")}
            >
              {tab.label}
            </button>
          );
        })}
      </nav>
    </div>
  );
}
