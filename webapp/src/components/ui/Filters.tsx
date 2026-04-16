import React from "react";
import Button from "./Button";

interface FiltersProps {
  children: React.ReactNode;
  onApply?: () => void;
  onReset?: () => void;
}

export default function Filters({ children, onApply, onReset }: FiltersProps) {
  return (
    <div className="flex flex-wrap items-end gap-4">
      {children}
      <div className="flex items-center gap-2">
        {onApply && (
          <Button variant="primary" size="sm" onClick={onApply}>
            Apply
          </Button>
        )}
        {onReset && (
          <Button variant="ghost" size="sm" onClick={onReset}>
            Reset
          </Button>
        )}
      </div>
    </div>
  );
}
