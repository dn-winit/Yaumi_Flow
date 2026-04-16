import { QueryClient } from "@tanstack/react-query";

// Single query client for the whole app
// staleTime: 5 min -- don't refetch within this window
// gcTime: 30 min -- keep in memory after unmount
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5 * 60 * 1000,
      gcTime: 30 * 60 * 1000,
      refetchOnWindowFocus: true,
      refetchOnReconnect: true,
      retry: 1,
    },
    mutations: {
      retry: 0,
    },
  },
});
