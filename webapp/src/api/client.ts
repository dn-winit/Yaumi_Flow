// Axios instance factory -- one client per service, consistent config

import axios, { AxiosInstance } from "axios";
import { API, ServiceKey } from "@/config/api";

const clients: Partial<Record<ServiceKey, AxiosInstance>> = {};

export function getClient(service: ServiceKey): AxiosInstance {
  if (!clients[service]) {
    clients[service] = axios.create({
      baseURL: API[service],
      timeout: 30000,
      headers: { "Content-Type": "application/json" },
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
