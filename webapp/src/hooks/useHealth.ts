import { useQuery } from "@tanstack/react-query";
import { dataImportApi } from "@/api/data-import";
import { forecastApi } from "@/api/forecast";
import { recommendedOrderApi } from "@/api/recommended-order";
import { supervisionApi } from "@/api/supervision";
import { analyticsApi } from "@/api/analytics";
import { tier } from "./refresh";
import type { HealthStatus } from "@/types/common";

const SERVICES = [
  { key: "data", label: "Data Import", fn: () => dataImportApi.getHealth() },
  { key: "forecast", label: "Forecasting", fn: () => forecastApi.getHealth() },
  { key: "orders", label: "Recommendations", fn: () => recommendedOrderApi.getHealth() },
  { key: "supervision", label: "Supervision", fn: () => supervisionApi.getHealth() },
  { key: "analytics", label: "AI Analytics", fn: () => analyticsApi.getHealth() },
];

async function checkAll(): Promise<HealthStatus[]> {
  const results = await Promise.allSettled(SERVICES.map((s) => s.fn()));
  return results.map((r, i) => ({
    service: SERVICES[i].label,
    status: r.status === "fulfilled" ? String((r.value as { status?: string }).status ?? "healthy") : "error",
    ok: r.status === "fulfilled",
  }));
}

export function useHealth() {
  const { data, isLoading, refetch } = useQuery({
    queryKey: ["health"],
    queryFn: checkAll,
    ...tier("health"),
  });
  return { services: data ?? [], loading: isLoading, refetch };
}
