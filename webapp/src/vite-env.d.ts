/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_DATA_IMPORT_URL: string;
  readonly VITE_FORECAST_URL: string;
  readonly VITE_RECOMMENDED_ORDER_URL: string;
  readonly VITE_SUPERVISION_URL: string;
  readonly VITE_ANALYTICS_URL: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
