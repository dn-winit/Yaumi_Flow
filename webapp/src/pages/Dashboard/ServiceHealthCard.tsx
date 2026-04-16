import React from "react";
import StatusDot from "@/components/ui/StatusDot";

interface ServiceHealthCardProps {
  service: string;
  status: string;
  ok: boolean;
}

export default function ServiceHealthCard({
  service,
  status,
  ok,
}: ServiceHealthCardProps) {
  return (
    <div className="bg-surface-raised rounded-xl shadow-1 border border-default p-4 flex items-center gap-3">
      <StatusDot status={ok ? "healthy" : "error"} />
      <div className="min-w-0">
        <p className="text-sm font-semibold text-text-primary truncate">
          {service}
        </p>
        <p className="text-xs text-text-tertiary truncate">{status}</p>
      </div>
    </div>
  );
}
