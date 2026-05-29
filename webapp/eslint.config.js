// Flat ESLint config -- ESLint 9+. Conservative rule set: catch real bugs
// (hooks misuse, undefined names, unused imports, exhaustive deps) without
// style churn. Formatting is owned by Prettier.

import js from "@eslint/js";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";

export default [
  {
    ignores: [
      "dist/**",
      "node_modules/**",
      "build/**",
      "coverage/**",
      ".vite/**",
      "*.config.js",
      "*.config.ts",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: "module",
      globals: {
        ...globals.browser,
      },
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      // React Hooks: catch the most common runtime bugs.
      ...reactHooks.configs.recommended.rules,

      // Fast-refresh hygiene -- warn (not error) so a one-off export
      // doesn't block CI.
      "react-refresh/only-export-components": [
        "warn",
        { allowConstantExport: true },
      ],

      // TS variant of no-unused-vars -- argument prefix _ is allowed.
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],

      // ``any`` is allowed when explicitly opted-in by the author.
      "@typescript-eslint/no-explicit-any": "warn",

      // Console.log slips through PRs -- warn only so error logging stays
      // ergonomic.
      "no-console": ["warn", { allow: ["warn", "error", "info"] }],
    },
  },
];
