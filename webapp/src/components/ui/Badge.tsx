import React from "react";
import type { Tone } from "@/lib/colorize";

type BadgeVariant = Tone;

interface BadgeProps {
  /** Preferred: pass a semantic tone (alias for `variant`). */
  tone?: Tone;
  variant?: BadgeVariant;
  children: React.ReactNode;
  className?: string;
}

const variantClasses: Record<BadgeVariant, string> = {
  success: "bg-success-100 text-success-700",
  warning: "bg-warning-100 text-warning-700",
  danger:  "bg-danger-100 text-danger-700",
  info:    "bg-info-100 text-info-700",
  neutral: "bg-neutral-100 text-neutral-600",
};

export default function Badge({
  tone,
  variant,
  children,
  className = "",
}: BadgeProps) {
  const resolved: BadgeVariant = tone ?? variant ?? "neutral";
  return (
    <span
      className={[
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-caption font-medium",
        variantClasses[resolved],
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {children}
    </span>
  );
}
