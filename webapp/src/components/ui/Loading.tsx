import React from "react";

type LoadingSize = "sm" | "md" | "lg";

interface LoadingProps {
  size?: LoadingSize;
  message?: string;
}

const sizeClasses: Record<LoadingSize, string> = {
  sm: "h-5 w-5",
  md: "h-8 w-8",
  lg: "h-12 w-12",
};

export default function Loading({ size = "md", message }: LoadingProps) {
  return (
    <div className="flex flex-col items-center justify-center py-8 gap-3">
      <svg
        className={`animate-spin text-brand-600 ${sizeClasses[size]}`}
        xmlns="http://www.w3.org/2000/svg"
        fill="none"
        viewBox="0 0 24 24"
      >
        <circle
          className="opacity-25"
          cx="12"
          cy="12"
          r="10"
          stroke="currentColor"
          strokeWidth="4"
        />
        <path
          className="opacity-75"
          fill="currentColor"
          d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
        />
      </svg>
      {message && <p className="text-body text-text-tertiary">{message}</p>}
    </div>
  );
}
