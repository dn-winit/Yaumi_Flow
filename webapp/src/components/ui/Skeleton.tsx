import type { CSSProperties } from "react";

interface SkeletonProps {
  className?: string;
  style?: CSSProperties;
}

export function Skeleton({ className = "", style }: SkeletonProps) {
  return (
    <div
      style={style}
      className={["animate-pulse bg-neutral-200 rounded-md", className]
        .filter(Boolean)
        .join(" ")}
    />
  );
}

export default Skeleton;
