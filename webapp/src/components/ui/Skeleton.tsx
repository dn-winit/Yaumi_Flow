interface SkeletonProps {
  className?: string;
}

export function Skeleton({ className = "" }: SkeletonProps) {
  return (
    <div
      className={["animate-pulse bg-neutral-200 rounded-md", className]
        .filter(Boolean)
        .join(" ")}
    />
  );
}

export default Skeleton;
