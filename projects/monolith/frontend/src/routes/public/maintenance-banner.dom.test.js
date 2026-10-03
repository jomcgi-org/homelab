// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createRawSnippet, mount, tick, unmount } from "svelte";
import { page } from "$app/state";
import PublicLayout from "./+layout.svelte";

const children = createRawSnippet(() => ({
  render: () => "<main>public route</main>",
}));

let instance;
let target;

beforeEach(() => {
  window.happyDOM.settings.disableCSSFileLoading = true;
  window.happyDOM.settings.handleDisabledFileLoadingAsSuccess = true;
  target = document.createElement("div");
  document.body.append(target);
});

afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  target.remove();
  page.url = new URL("https://public.example.test/");
});

async function renderLayout(path, maintenanceBanner) {
  page.url = new URL(path, "https://public.example.test");
  instance = mount(PublicLayout, {
    target,
    props: { data: { maintenanceBanner }, children },
  });
  await tick();
}

describe("public maintenance banner", () => {
  it.each([
    "/",
    "/public",
    "/blog/125b-on-a-4090",
    "/engineering",
    "/app/hikes",
    "/slop/factory",
  ])(
    "leaves no empty banner on %s when the notice is cleared",
    async (path) => {
      await renderLayout(path, "");
      expect(target.querySelector(".maintenance-banner")).toBeNull();
      expect(target.querySelector('[role="status"]')).toBeNull();
      expect(target.querySelector("main")?.textContent).toBe("public route");
    },
  );

  it.each([null, undefined])("omits a missing notice (%s)", async (value) => {
    await renderLayout("/", value);
    expect(target.querySelector(".maintenance-banner")).toBeNull();
  });

  it("still supports an explicitly configured maintenance notice", async () => {
    await renderLayout("/", "Scheduled maintenance tonight.");
    expect(target.querySelector('[role="status"]')?.textContent).toBe(
      "Scheduled maintenance tonight.",
    );
  });

  it.each(["/slop/factory", "/public/slop/factory"])(
    "keeps the existing independent Slop surface on %s",
    async (path) => {
      await renderLayout(path, "Scheduled maintenance tonight.");
      expect(target.querySelector(".maintenance-banner")).toBeNull();
    },
  );
});
