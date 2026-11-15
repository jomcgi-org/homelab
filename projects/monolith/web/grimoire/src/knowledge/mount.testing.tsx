/** Renders one knowledge panel on its own against the fixture backend. */
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

export function mount(ui: ReactNode, routes: Routes) {
  current = installBackend(routes);
  const client = createQueryClient();
  const view = render(
    <MemoryRouter>
      <Providers client={client}>{ui}</Providers>
    </MemoryRouter>,
  );
  return { user: userEvent.setup(), backend: current, client, view };
}
