// Axios instance factory -- one client per service, consistent config.

import axios, { AxiosInstance } from "axios";
import { API, ServiceKey } from "@/config/api";

// Single source of truth for request timeouts. Keep narrow: default for
// reads, `heavy` for any mutation that triggers LLM calls or full
// recommendation generation (3 min upper bound matches backend retries).
export const TIMEOUTS = {
  default: 30_000,
  heavy: 180_000,
} as const;

const clients: Partial<Record<ServiceKey, AxiosInstance>> = {};

export function getClient(service: ServiceKey): AxiosInstance {
  if (!clients[service]) {
    clients[service] = axios.create({
      baseURL: API[service],
      timeout: TIMEOUTS.default,
      headers: { "Content-Type": "application/json" },
      // Serialize array params as repeated `?key=v1&key=v2` (no brackets)
      // so FastAPI's `Query(default=[])` parameters bind correctly.
      paramsSerializer: { indexes: null },
    });

    clients[service]!.interceptors.response.use(
      (res) => res,
      (err) => {
        const msg =
          err.response?.data?.detail ||
          err.response?.data?.message ||
          err.message ||
          "Request failed";
        return Promise.reject(new Error(msg));
      }
    );
  }
  return clients[service]!;
}
