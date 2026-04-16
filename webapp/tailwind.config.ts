/**
 * Tailwind config — extends the framework theme from `src/theme/tokens.ts`.
 *
 * Loaded by Tailwind v4 via the `@config` directive in `src/index.css`.
 * This is the sole bridge between the typed token module and utility classes
 * (`bg-brand-600`, `text-success-700`, `shadow-3`, etc.).
 */
import type { Config } from "tailwindcss";
import { tokens } from "./src/theme/tokens";

const { color, radius, shadow, space, font, motion, z } = tokens;

const config: Config = {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        brand:   color.brand,
        accent:  color.accent,
        success: color.success,
        warning: color.warning,
        danger:  color.danger,
        info:    color.info,
        neutral: color.neutral,
        surface: color.surface,
      },
      textColor: {
        DEFAULT:   color.text.primary,
        primary:   color.text.primary,
        secondary: color.text.secondary,
        tertiary:  color.text.tertiary,
        inverse:   color.text.inverse,
        brand:     color.brand,
        accent:    color.accent,
      },
      borderColor: {
        DEFAULT: color.border.default,
        subtle:  color.border.subtle,
        strong:  color.border.strong,
        brand:   color.brand,
        accent:  color.accent,
      },
      borderRadius: radius,
      boxShadow: {
        1: shadow[1],
        2: shadow[2],
        3: shadow[3],
        4: shadow[4],
        5: shadow[5],
      },
      spacing: space,
      fontFamily: {
        sans: font.family.sans.split(",").map((s) => s.trim().replace(/^"|"$/g, "")),
        mono: font.family.mono.split(",").map((s) => s.trim().replace(/^"|"$/g, "")),
      },
      fontSize: {
        caption: font.size.caption,
        label:   font.size.label,
        body:    font.size.body,
        heading: font.size.heading,
        title:   font.size.title,
        display: font.size.display,
      },
      fontWeight: {
        normal:   font.weight.normal,
        medium:   font.weight.medium,
        semibold: font.weight.semibold,
        bold:     font.weight.bold,
      },
      lineHeight: font.lineHeight,
      transitionDuration: {
        fast: motion.fast,
        base: motion.base,
        slow: motion.slow,
      },
      zIndex: {
        dropdown: String(z.dropdown),
        sticky:   String(z.sticky),
        drawer:   String(z.drawer),
        modal:    String(z.modal),
        toast:    String(z.toast),
      },
    },
  },
};

export default config;
