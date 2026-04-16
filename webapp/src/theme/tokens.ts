/**
 * Design tokens — single source of truth for the UI.
 *
 * Consumed by:
 *   - `tailwind.config.ts` (extends Tailwind's theme)
 *   - `src/index.css` (mirrored as CSS custom properties for runtime access)
 *   - `src/components/charts/theme.ts` (Recharts palette & shared props)
 *
 * Keep values in raw CSS units (px / unitless) so both Tailwind and CSS vars
 * can consume them without translation.
 */

export const tokens = {
  color: {
    brand: {
      50:  "#FBE9EC",
      100: "#F4C2C9",
      200: "#EA8E9C",
      300: "#DD5C71",
      400: "#D03048",
      500: "#C8102E",
      600: "#B00E28",
      700: "#8C0A20",
      800: "#6E0819",
      900: "#4F0612",
    },
    accent: {
      50:  "#FDF6E3",
      100: "#F8E5A8",
      200: "#F2D070",
      300: "#ECBF4A",
      400: "#E8B23A",
      500: "#D9A12B",
      600: "#C9962A",
      700: "#9F751E",
      800: "#785815",
      900: "#523B0E",
    },
    success: {
      50:  "#f0fdf4",
      100: "#dcfce7",
      500: "#22c55e",
      600: "#16a34a",
      700: "#15803d",
    },
    warning: {
      50:  "#fffbeb",
      100: "#fef3c7",
      500: "#f59e0b",
      600: "#d97706",
      700: "#b45309",
    },
    danger: {
      50:  "#fef2f2",
      100: "#fee2e2",
      500: "#ef4444",
      600: "#dc2626",
      700: "#b91c1c",
    },
    info: {
      50:  "#ecfeff",
      100: "#cffafe",
      500: "#06b6d4",
      600: "#0891b2",
      700: "#0e7490",
    },
    neutral: {
      0:   "#ffffff",
      50:  "#f8fafc",
      100: "#f1f5f9",
      200: "#e2e8f0",
      300: "#cbd5e1",
      400: "#94a3b8",
      500: "#64748b",
      600: "#475569",
      700: "#334155",
      800: "#1e293b",
      900: "#0f172a",
    },
    surface: {
      base:    "#f8fafc", // app background
      raised:  "#ffffff", // cards, modals
      sunken:  "#f1f5f9", // muted panels / stat tiles
      overlay: "rgba(15, 23, 42, 0.5)", // modal scrim
    },
    text: {
      primary:   "#0f172a",
      secondary: "#475569",
      tertiary:  "#94a3b8",
      inverse:   "#ffffff",
    },
    border: {
      subtle:  "#f1f5f9",
      default: "#e2e8f0",
      strong:  "#cbd5e1",
    },
  },

  radius: {
    sm:   "0.25rem",
    md:   "0.5rem",
    lg:   "0.75rem",
    xl:   "1rem",
    full: "9999px",
  },

  // Elevation tiers 1..5
  shadow: {
    1: "0 1px 2px 0 rgba(15, 23, 42, 0.05)",
    2: "0 1px 3px 0 rgba(15, 23, 42, 0.08), 0 1px 2px -1px rgba(15, 23, 42, 0.06)",
    3: "0 4px 6px -1px rgba(15, 23, 42, 0.08), 0 2px 4px -2px rgba(15, 23, 42, 0.06)",
    4: "0 10px 15px -3px rgba(15, 23, 42, 0.10), 0 4px 6px -4px rgba(15, 23, 42, 0.08)",
    5: "0 20px 25px -5px rgba(15, 23, 42, 0.12), 0 8px 10px -6px rgba(15, 23, 42, 0.08)",
  },

  // 4px baseline grid
  space: {
    0:  "0",
    1:  "0.25rem",
    2:  "0.5rem",
    3:  "0.75rem",
    4:  "1rem",
    5:  "1.25rem",
    6:  "1.5rem",
    8:  "2rem",
    10: "2.5rem",
    12: "3rem",
  },

  font: {
    family: {
      sans: '"Inter", system-ui, -apple-system, "Segoe UI", sans-serif',
      mono: '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, monospace',
    },
    size: {
      caption: "0.75rem",  // 12px
      label:   "0.8125rem", // 13px
      body:    "0.875rem", // 14px
      heading: "1rem",     // 16px
      title:   "1.125rem", // 18px
      display: "1.5rem",   // 24px
    },
    weight: {
      normal:   "400",
      medium:   "500",
      semibold: "600",
      bold:     "700",
    },
    lineHeight: {
      tight:   "1.25",
      normal:  "1.5",
      relaxed: "1.75",
    },
  },

  motion: {
    fast: "120ms",
    base: "180ms",
    slow: "260ms",
  },

  z: {
    dropdown: 1000,
    sticky:   1020,
    drawer:   1030,
    modal:    1040,
    toast:    1060,
  },

  chart: {
    // Note: palette is assembled in `src/components/charts/theme.ts` by
    // referencing the brand/accent/status scales above, so there is no
    // duplicated hex here. Kept as an empty tuple for structural parity.
    palette: [] as readonly string[],
    axis: {
      stroke:   "#cbd5e1", // neutral-300
      fill:     "#64748b", // neutral-500 (tick text)
      fontSize: 12,
    },
    grid: {
      stroke: "#e2e8f0", // neutral-200
    },
    tooltip: {
      background: "#ffffff",
      border:     "#e2e8f0",
      color:      "#0f172a",
      radius:     "0.5rem",
      shadow:     "0 4px 6px -1px rgba(15, 23, 42, 0.08), 0 2px 4px -2px rgba(15, 23, 42, 0.06)",
    },
  },
} as const;

export type Tokens = typeof tokens;
