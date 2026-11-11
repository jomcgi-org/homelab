/**
 * Test entry for screens: mounts the real app at a path against the fixture
 * backend. Query by role and name, as a player or a screen reader would.
 */
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach } from "vitest";
import { App } from "../App";
import { createQueryClient, Providers } from "../app/Providers";
import {
  installBackend,
  type Backend,
  type OtherRoutes,
  type Routes,
} from "./backend";

let current: Backend | undefined;
afterEach(() => {
  current?.restore();
  current = undefined;
});

export function openApp(routes: Routes, path = "/", other: OtherRoutes = {}) {
  current = installBackend(routes, other);
  render(
    <MemoryRouter initialEntries={[path]}>
      <Providers client={createQueryClient()}>
        <App />
      </Providers>
    </MemoryRouter>,
  );
  return { user: userEvent.setup(), backend: current };
}

/** A toast renders its text twice: once visibly, once for screen readers. */
export async function findToast(text: string) {
  const [first] = await screen.findAllByText(text);
  return first!;
}

/**
 * Picks an option in a Selector or Typeahead. The listbox stays in the DOM
 * while closed, so the option is looked up inside the one this control owns.
 * Pointer events are sent directly: the menu commits on pointer-up, which the
 * test DOM does not synthesise from a click.
 */
export async function chooseOption(
  scope: HTMLElement,
  control: string,
  option: string,
) {
  const combobox = within(scope).getByRole("combobox", { name: control });
  fireEvent.click(combobox);
  const listbox = document.getElementById(
    combobox.getAttribute("aria-controls") ?? "",
  );
  const target = within(listbox ?? document.body).getByRole("option", {
    name: option,
  });
  fireEvent.pointerDown(target);
  fireEvent.mouseDown(target);
  fireEvent.pointerUp(target);
  fireEvent.mouseUp(target);
  fireEvent.click(target);
}
