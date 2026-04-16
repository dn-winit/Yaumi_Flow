import React from "react";

type ButtonVariant = "primary" | "secondary" | "danger" | "ghost";
type ButtonSize = "sm" | "md" | "lg";

interface ButtonProps {
  variant?: ButtonVariant;
  size?: ButtonSize;
  loading?: boolean;
  disabled?: boolean;
  children: React.ReactNode;
  onClick?: (e: React.MouseEvent<HTMLButtonElement>) => void;
  className?: string;
  type?: "button" | "submit" | "reset";
}

const variantClasses: Record<ButtonVariant, string> = {
  primary:
    "bg-brand-600 text-inverse hover:bg-brand-700 active:bg-brand-700 focus:ring-brand-500",
  secondary:
    "bg-surface-raised text-text-secondary border border-default hover:bg-surface-sunken active:bg-surface-sunken focus:ring-brand-500",
  danger:
    "bg-danger-600 text-inverse hover:bg-danger-700 active:bg-danger-700 focus:ring-danger-500",
  ghost:
    "bg-transparent text-text-secondary hover:bg-surface-sunken active:bg-surface-sunken focus:ring-brand-500",
};

const sizeClasses: Record<ButtonSize, string> = {
  sm: "px-3 py-1 text-body",
  md: "px-4 py-2 text-body",
  lg: "px-6 py-3 text-heading",
};

const Spinner = () => (
  <svg
    className="animate-spin -ml-1 mr-2 h-4 w-4 text-current"
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
);

export default function Button({
  variant = "primary",
  size = "md",
  loading = false,
  disabled = false,
  children,
  onClick,
  className = "",
  type = "button",
}: ButtonProps) {
  const isDisabled = disabled || loading;

  return (
    <button
      type={type}
      onClick={onClick}
      disabled={isDisabled}
      className={[
        "inline-flex items-center justify-center rounded-lg font-medium transition-all duration-base focus:outline-none focus:ring-2 focus:ring-offset-2",
        variantClasses[variant],
        sizeClasses[size],
        isDisabled ? "opacity-50 cursor-not-allowed" : "cursor-pointer",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {loading && <Spinner />}
      {children}
    </button>
  );
}
