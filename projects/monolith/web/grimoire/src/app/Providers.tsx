import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { LinkProvider } from "@astryxdesign/core/Link";
import { ToastViewport } from "@astryxdesign/core/Toast";
import { ApiError } from "../api/client";
import { AppearanceProvider } from "./Appearance";
import { RouterLink } from "./RouterLink";

export function createQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // A refusal will not change on retry; a dropped connection might.
        retry: (count, error) => !(error instanceof ApiError) && count < 2,
        staleTime: 5_000,
      },
      mutations: { retry: false },
    },
  });
}

const queryClient = createQueryClient();

/** Theme, links, server state and toasts. Sits inside the router. */
export function Providers({
  children,
  client = queryClient,
}: {
  children: ReactNode;
  client?: QueryClient;
}) {
  return (
    <AppearanceProvider>
      <LinkProvider component={RouterLink}>
        <QueryClientProvider client={client}>
          <ToastViewport position="bottomEnd">{children}</ToastViewport>
        </QueryClientProvider>
      </LinkProvider>
    </AppearanceProvider>
  );
}
