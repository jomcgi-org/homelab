// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Homepage from "./+page.svelte";

let instance;
let target;

beforeEach(() => {
  window.happyDOM.settings.disableCSSFileLoading = true;
  window.happyDOM.settings.handleDisabledFileLoadingAsSuccess = true;
  vi.stubGlobal(
    "IntersectionObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  target = document.createElement("div");
  document.body.append(target);
});

afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  target.remove();
  vi.unstubAllGlobals();
});

describe("homepage calls to action", () => {
  it("leads with Slop Factory and keeps the homelab as the secondary destination", async () => {
    instance = mount(Homepage, { target, props: { data: { stats: null } } });
    await tick();

    const links = target.querySelectorAll(".hero-cta-row a");
    expect(links).toHaveLength(2);
    const [primary, secondary] = links;
    expect(primary.classList.contains("btn-primary")).toBe(true);
    expect(primary.getAttribute("href")).toBe("/slop/factory");
    expect(primary.getAttribute("aria-label")).toBe("Slop Factory");
    expect(primary.textContent).toContain("Slop Factory");
    expect(primary.querySelector("s")?.textContent).toBe("Software");
    expect(primary.querySelector("s")?.getAttribute("aria-hidden")).toBe(
      "true",
    );
    expect(secondary.classList.contains("btn-secondary")).toBe(true);
    expect(secondary.getAttribute("href")).toBe("#homelab");
    expect(target.querySelector("#homelab")).not.toBeNull();
  });
});
