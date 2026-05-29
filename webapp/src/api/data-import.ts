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
  LastActiveDateResponse,
  ReportingPeriod,
} from "@/types/data-import";
import type { DataSummary } from "@/types/common";

const c = () => getClient("dataImport");

// Empty arrays dropped so "no filter" === absent param.
function filterParams(f?: Partial<DashboardFilters>): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  if (f?.warehouse_codes?.length) out.warehouse_codes = f.warehouse_codes;
  if (f?.route_codes?.length) out.route_codes = f.route_codes;
  if (f?.category_codes?.length) out.category_codes = f.category_codes;
  if (f?.item_codes?.length) out.item_codes = f.item_codes;
  return out;
}

export const dataImportApi = {
  getStatus: () =>
    c()
      .get<DataStatusResponse>("/status")
      .then((r) => r.data),

  getSummary: () =>
    c()
      .get<DataSummary>("/summary")
      .then((r) => r.data),

  getSalesOverview: (period: ReportingPeriod, filters?: Partial<DashboardFilters>) =>
    c()
      .get<SalesOverviewResponse>("/eda/sales", {
        params: {
          start_date: period.start_date,
          end_date: period.end_date,
          ...filterParams(filters),
        },
      })
      .then((r) => r.data),

  getItemCatalog: () =>
    c()
      .get<ItemCatalogResponse>("/eda/items")
      .then((r) => r.data),

  getLastActiveDate: () =>
    c()
      .get<LastActiveDateResponse>("/eda/last-active-date")
      .then((r) => r.data),

  getBusinessKpis: (period: ReportingPeriod, filters?: Partial<DashboardFilters>) =>
    c()
      .get<BusinessKpis>("/eda/business-kpis", {
        params: {
          start_date: period.start_date,
          end_date: period.end_date,
          ...filterParams(filters),
        },
      })
      .then((r) => r.data),

  getFilterDimensions: (filters?: Partial<DashboardFilters>) =>
    c()
      .get<FilterDimensions>("/eda/filter-dimensions", {
        // Send full selection so backend can return trimmed_selections for the FilterBar.
        params: filterParams({
          warehouse_codes: filters?.warehouse_codes,
          route_codes: filters?.route_codes,
          category_codes: filters?.category_codes,
          item_codes: filters?.item_codes,
        }),
      })
      .then((r) => r.data),

  getItemStats: (itemCode: string, routeCode?: string) =>
    c()
      .get<ItemStatsResponse>("/eda/item-stats", {
        params: { item_code: itemCode, ...(routeCode ? { route_code: routeCode } : {}) },
      })
      .then((r) => r.data),

  importDataset: (dataset: string, mode = "incremental") =>
    c()
      .post<ImportResponse>("/import", { dataset, mode })
      .then((r) => r.data),

  importAll: (mode = "incremental") =>
    c()
      .post<ImportAllResponse>("/import-all", { mode })
      .then((r) => r.data),
};
