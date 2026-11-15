/** Test entry for a panel on its own: the app's providers and the fixture backend. */
import type { ReactNode } from "react";
import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach } from "vitest";
import { createQueryClient, Providers } from "../app/Providers";
import { installBackend, type Backend, type Routes } from "../test/backend";

let current: Backend | undefined;
afterEach(() => {
  current?.restore();
  current = undefined;
});

export function renderPanel(panel: ReactNode, routes: Routes = {}) {
  current = installBackend(routes);
  const client = createQueryClient();
  const wrap = (node: ReactNode) => (
    <MemoryRouter>
      <Providers client={client}>{node}</Providers>
    </MemoryRouter>
  );
  const view = render(wrap(panel));
  return {
    user: userEvent.setup(),
    backend: current,
    rerender: (next: ReactNode) => view.rerender(wrap(next)),
  };
}
