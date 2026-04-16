import { useState } from "react";
import Tabs from "@/components/ui/Tabs";
import InfoPanel from "@/components/ui/InfoPanel";
import { SUPERVISION_INFO } from "@/config/module-info";
import LiveSessionTab from "@/pages/Supervision/LiveSession/LiveSessionTab";
import ReviewTab from "@/pages/Supervision/Review/ReviewTab";

const TABS = [
  { key: "live", label: "Live Session" },
  { key: "review", label: "Review" },
];

export default function SupervisionTab() {
  const [activeTab, setActiveTab] = useState("live");

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <div className="flex-1">
          <Tabs tabs={TABS} activeTab={activeTab} onTabChange={setActiveTab} />
        </div>
        <InfoPanel {...SUPERVISION_INFO} />
      </div>
      {activeTab === "live" && <LiveSessionTab />}
      {activeTab === "review" && <ReviewTab />}
    </div>
  );
}
