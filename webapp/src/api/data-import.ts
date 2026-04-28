import { getClient } from "./client";
import type {
  DataStatusResponse,
  ImportResponse,
  ImportAllResponse,
  SalesOverviewResponse,
  ItemCatalogResponse,
  ItemStatsResponse,
  BusinessKpis,
  DashboardFilters,
  FilterDimensions,
  ForecastRowsResponse,
  LookbackWindowResponse,
} from "@/types/data-import";
import type { DataSummary } from "@/types/common";

const c = () => getClient("dataImport");

// Build query params from a DashboardFilters object. Empty arrays are
// dropped so the URL stays clean ("no filter" === absent param).
function filterParams(f?: Partial<DashboardFilters>): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  if (f?.warehouse_codes?.length) out.warehouse_codes = f.warehouse_codes;
  if (f?.route_codes?.length) out.route_codes = f.route_codes;
  if (f?.category_codes?.length) out.category_codes = f.category_codes;
  if (f?.item_codes?.length) out.item_codes = f.item_codes;
  return out;
}

export const dataImportApi = {
  getStatus: () => c().get<DataStatusResponse>("/status").then((r) => r.data),

  getSummary: () => c().get<DataSummary>("/summary").then((r) => r.data),

  getSalesOverview: (lookback?: string, filters?: Partial<DashboardFilters>) =>
    c()
      .get<SalesOverviewResponse>("/eda/sales", {
        params: { ...(lookback ? { lookback } : {}), ...filterParams(filters) },
      })
      .then((r) => r.data),

  getItemCatalog: () => c().get<ItemCatalogResponse>("/eda/items").then((r) => r.data),

  getBusinessKpis: (lookback?: string, filters?: Partial<DashboardFilters>) =>
    c()
      .get<BusinessKpis>("/eda/business-kpis", {
        params: { ...(lookback ? { lookback } : {}), ...filterParams(filters) },
      })
      .then((r) => r.data),

  getForecastRows: (lookback?: string, filters?: Partial<DashboardFilters>) =>
    c()
      .get<ForecastRowsResponse>("/eda/forecast-rows", {
        params: { ...(lookback ? { lookback } : {}), ...filterParams(filters) },
      })
      .then((r) => r.data),

  getFilterDimensions: (filters?: Partial<DashboardFilters>) =>
    c()
      .get<FilterDimensions>("/eda/filter-dimensions", {
        // The endpoint only consumes warehouse/route/category (items are leaves).
        params: filterParams({
          warehouse_codes: filters?.warehouse_codes,
          route_codes: filters?.route_codes,
          category_codes: filters?.category_codes,
          item_codes: [],
        }),
      })
      .then((r) => r.data),

  getItemStats: (itemCode: string, routeCode?: string) =>
    c()
      .get<ItemStatsResponse>("/eda/item-stats", {
        params: { item_code: itemCode, ...(routeCode ? { route_code: routeCode } : {}) },
      })
      .then((r) => r.data),

  getLookbackWindow: (lookback: string) =>
    c()
      .get<LookbackWindowResponse>("/eda/lookback-window", { params: { lookback } })
      .then((r) => r.data),

  importDataset: (dataset: string, mode = "incremental") =>
    c().post<ImportResponse>("/import", { dataset, mode }).then((r) => r.data),

  importAll: (mode = "incremental") =>
    c().post<ImportAllResponse>("/import-all", { mode }).then((r) => r.data),
};
