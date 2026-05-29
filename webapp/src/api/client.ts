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

const REQUEST_ID_HEADER = "X-Request-ID";

/**
 * Generate a hex request id without pulling in a uuid library. Combines
 * timestamp + 12 hex chars of randomness — plenty unique for the per-tab,
 * per-session correlation we need to grep a single click across the 5
 * backend log streams.
 *
 * Falls back to ``crypto.getRandomValues`` (available in every modern
 * browser + Node test runners); never throws on absence.
 */
function generateRequestId(): string {
  const ts = Date.now().toString(36);
  const bytes = new Uint8Array(8);
  if (typeof crypto !== "undefined" && crypto.getRandomValues) {
    crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  }
  const rand = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${ts}-${rand}`;
}

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

    // Request-id propagation: every outbound request carries an
    // ``X-Request-ID`` header so the backend's common.api_middleware can
    // bind it into the structured log context. A single click on the UI
    // therefore produces one correlated id across all 5 backend log
    // streams. Callers may override by setting ``config.headers
    // [REQUEST_ID_HEADER]`` explicitly before .request().
    clients[service]!.interceptors.request.use((config) => {
      const headers = config.headers ?? {};
      if (!headers[REQUEST_ID_HEADER] && !headers[REQUEST_ID_HEADER.toLowerCase()]) {
        // Axios headers accept indexing; cast keeps types narrow without
        // pulling in axios's header subtype.
        (headers as Record<string, string>)[REQUEST_ID_HEADER] = generateRequestId();
      }
      config.headers = headers;
      return config;
    });

    clients[service]!.interceptors.response.use(
      (res) => res,
      (err) => {
        const msg =
          err.response?.data?.detail ||
          err.response?.data?.message ||
          err.response?.data?.error ||
          err.message ||
          "Request failed";
        // Preserve axios's diagnostic fields (``response``, ``code``,
        // ``config``, ``status``) on the rejected error so retry / refresh
        // logic anywhere upstream can still inspect them. Earlier wrap
        // discarded these silently; callers reading ``.response?.status``
        // would have got ``undefined``.
        const requestId = err.response?.data?.request_id;
        const wrapped = new Error(msg) as Error & {
          requestId?: string;
          status?: number;
          code?: string;
          response?: typeof err.response;
          config?: typeof err.config;
          isAxiosError?: boolean;
        };
        if (typeof requestId === "string") wrapped.requestId = requestId;
        wrapped.status = err.response?.status;
        wrapped.code = err.code;
        wrapped.response = err.response;
        wrapped.config = err.config;
        wrapped.isAxiosError = true;
        return Promise.reject(wrapped);
      },
    );
  }
  return clients[service]!;
}
