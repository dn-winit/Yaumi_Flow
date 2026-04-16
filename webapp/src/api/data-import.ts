import { getClient } from "./client";
import type {
  DataStatusResponse,
  ImportResponse,
  ImportAllResponse,
  DataHealthResponse,
  SalesOverviewResponse,
  CustomerOverviewResponse,
  ItemCatalogResponse,
  ItemStatsResponse,
  BusinessKpis,
} from "@/types/data-import";
import type { DataSummary } from "@/types/common";

const c = () => getClient("dataImport");

export const dataImportApi = {
  getStatus: () => c().get<DataStatusResponse>("/status").then((r) => r.data),

  getSummary: () => c().get<DataSummary>("/summary").then((r) => r.data),

  getSalesOverview: () => c().get<SalesOverviewResponse>("/eda/sales").then((r) => r.data),

  getItemCatalog: () => c().get<ItemCatalogResponse>("/eda/items").then((r) => r.data),

  getBusinessKpis: () => c().get<BusinessKpis>("/eda/business-kpis").then((r) => r.data),

  getItemStats: (itemCode: string, routeCode?: string) =>
    c()
      .get<ItemStatsResponse>("/eda/item-stats", {
        params: { item_code: itemCode, ...(routeCode ? { route_code: routeCode } : {}) },
      })
      .then((r) => r.data),

  getCustomerOverview: (lookbackDays = 90) =>
    c().get<CustomerOverviewResponse>("/eda/customers", { params: { lookback_days: lookbackDays } }).then((r) => r.data),

  refreshEda: () => c().post("/eda/refresh").then((r) => r.data),

  importDataset: (dataset: string, mode = "incremental") =>
    c().post<ImportResponse>("/import", { dataset, mode }).then((r) => r.data),

  importAll: (mode = "incremental") =>
    c().post<ImportAllResponse>("/import-all", { mode }).then((r) => r.data),

  getHealth: () => c().get<DataHealthResponse>("/health").then((r) => r.data),
};
