import { useState } from "react";
import Tabs from "@/components/ui/Tabs";
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
      <Tabs tabs={TABS} activeTab={activeTab} onTabChange={setActiveTab} />
      {activeTab === "live" && <LiveSessionTab />}
      {activeTab === "review" && <ReviewTab />}
    </div>
  );
}
