import React from "react";

type Status = "healthy" | "degraded" | "error" | "unknown";

interface StatusDotProps {
  status: Status;
}

const statusClasses: Record<Status, string> = {
  healthy: "bg-success-500",
  degraded: "bg-warning-500",
  error: "bg-danger-600",
  unknown: "bg-neutral-400",
};

export default function StatusDot({ status }: StatusDotProps) {
  return (
    <span className="relative inline-flex h-3 w-3">
      {status === "healthy" && (
        <span className="absolute inline-flex h-full w-full rounded-full bg-success-500 opacity-75 animate-ping" />
      )}
      <span
        className={`relative inline-flex h-3 w-3 rounded-full ${statusClasses[status]}`}
      />
    </span>
  );
}
