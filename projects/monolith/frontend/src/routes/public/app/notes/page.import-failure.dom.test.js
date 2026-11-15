// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { mount, unmount } from "svelte";
import Page from "./+page.svelte";

vi.mock("$app/environment", () => ({ browser: true }));
vi.mock("$lib/public/notes/GraphView.svelte", () => {
  throw new Error("Failed to fetch dynamically imported module");
});

let instance;
afterEach(async () => {
  if (instance) await unmount(instance);
  document.body.replaceChildren();
  vi.restoreAllMocks();
});

it("reloads the document when a cached module failure cannot be retried", async () => {
  const reload = vi
    .spyOn(window.location, "reload")
    .mockImplementation(() => {});
  instance = mount(Page, { target: document.body });
  await vi.waitFor(() =>
    expect(document.body.textContent).toContain(
      "The graph is unavailable right now.",
    ),
  );
  [...document.querySelectorAll("button")]
    .find((button) => button.textContent.includes("TRY AGAIN"))
    .click();
  expect(reload).toHaveBeenCalledOnce();
});
