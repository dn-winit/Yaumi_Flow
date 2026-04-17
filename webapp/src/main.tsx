import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import { queryClient } from "./config/query-client";
import { ToastProvider } from "./hooks/useToast";
import ToastContainer from "./components/ui/Toast";
import "./index.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <BrowserRouter>
          <App />
        </BrowserRouter>
        <ToastContainer />
      </ToastProvider>
    </QueryClientProvider>
  </StrictMode>
);
