// API service registry -- all URLs from env, zero hardcoding
// In production (Docker): env vars are empty, so paths are relative (nginx proxies)
// In development: env vars point to http://localhost:PORT

const base = (envUrl: string, path: string) =>
  envUrl ? `${envUrl}${path}` : path;

export const API = {
  dataImport: base(import.meta.env.VITE_DATA_IMPORT_URL, "/api/v1/data"),
  forecast: base(import.meta.env.VITE_FORECAST_URL, "/api/v1/forecast"),
  recommendedOrder: base(import.meta.env.VITE_RECOMMENDED_ORDER_URL, "/api/v1/recommended-order"),
  supervision: base(import.meta.env.VITE_SUPERVISION_URL, "/api/v1/supervision"),
  analytics: base(import.meta.env.VITE_ANALYTICS_URL, "/api/v1/analytics"),
} as const;

export type ServiceKey = keyof typeof API;
