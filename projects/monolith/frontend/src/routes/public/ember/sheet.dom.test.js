// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Route from "../../../../test/ProfileRoute.svelte";
import { setPage } from "../../../../test/profile-page.svelte.js";
import { servingWakeMs } from "$lib/public/fcstory/metrics.js";

vi.mock("$app/state", () => import("../../../../test/profile-page.svelte.js"));
vi.mock("$app/stores", () => import("../../../../test/profile-stores.js"));

let instance;
let target;
beforeEach(() => {
  window.happyDOM.settings.disableCSSFileLoading = true;
  window.happyDOM.settings.handleDisabledFileLoadingAsSuccess = true;
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query) => ({
      matches: query.includes("reduced-motion"),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
  target = document.createElement("div");
  document.body.append(target);
});
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  target.remove();
  document.documentElement.removeAttribute("data-theme");
  localStorage.clear();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
async function render(path, surface) {
  setPage(`https://jomcgi.dev${path}`, { hasBlog: true });
  instance = mount(Route, { target, props: { surface } });
  await tick();
}

describe("Ember production components in the real layouts", () => {
  it.each([
    ["/ember", "ember", "ember"],
    ["/ember/bazel", "bazel-fallback", "bazel"],
    ["/ember/postgres", "postgres-fallback", "postgres"],
    ["/public/ember/", "ember", "ember"],
    ["/public/ember/bazel", "bazel-fallback", "bazel"],
    ["/public/ember/postgres", "postgres-fallback", "postgres"],
  ])("renders shared sheet chrome at %s", async (path, surface, crumb) => {
    await render(path, surface);
    expect(target.querySelectorAll("main.td.sheet.ember-sheet")).toHaveLength(
      1,
    );
    expect(target.querySelectorAll(".chrome, button.scheme")).toHaveLength(2);
    expect(target.querySelector(".md-nav, .topbar, .ember-site")).toBeNull();
    expect(target.querySelector(".chrome .trail-slot .trail")).not.toBeNull();
    expect(target.querySelector(".spine .trail-dock .trail")).not.toBeNull();
    expect(target.querySelector(".trail [aria-current=page]").textContent).toBe(
      crumb,
    );
    expect(
      target.querySelector('nav[aria-label="Site"] a[href="/engineering"]'),
    ).not.toBeNull();
    expect(target.querySelector(".content.editions > .edition")).not.toBeNull();
    expect(
      target.querySelector('a[href="/ember/firecracker#replay"]'),
    ).not.toBeNull();
    const toggle = target.querySelector("button.scheme");
    toggle.click();
    await tick();
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("td-theme")).toBe("dark");
    await unmount(instance);
    await render(path, surface);
    expect(
      target.querySelector("button.scheme").getAttribute("aria-label"),
    ).toBe("Switch to day scheme");
    if (surface !== "ember") {
      expect(target.querySelector(".ed-lead p").textContent).toBe(
        "Continue to the recorded replay.",
      );
      expect(target.querySelector("h1, form, input")).toBeNull();
    }
  });

  it("keeps landing copy, links, SEO and final metrics with reduced motion", async () => {
    const raf = vi.fn();
    vi.stubGlobal("requestAnimationFrame", raf);
    await render("/ember?from=engineering", "ember");
    expect(target.querySelector("h1").textContent).toBe("Ember");
    expect(target.querySelector(".lede").textContent).toContain(
      "Every job gets its own tiny virtual machine.",
    );
    expect(target.querySelector(".stats").textContent).toContain(
      `${servingWakeMs} ms`,
    );
    expect(target.querySelector(".stats").textContent).toContain("~22 ms");
    expect(raf).not.toHaveBeenCalled();
    expect(document.title).toBe(
      "Ember · a workload orchestrator on Firecracker microVMs",
    );
    expect(
      document.head.querySelector('meta[name="description"]').content,
    ).toContain(`${servingWakeMs} ms`);
    expect(target.querySelector('a[href="/ember/agents"]')).not.toBeNull();
    expect(target.querySelector('a[href="/ember/postgres"]')).toBeNull();
    expect(target.textContent).not.toContain("right now");
    expect(target.querySelectorAll('details[name="em-classes"]')).toHaveLength(
      7,
    );
    expect(
      target.querySelector(
        '.arch[tabindex="0"][role="region"] svg[role="img"]',
      ),
    ).not.toBeNull();
    expect(
      target.querySelector('.index.strip a[href="#classes"]'),
    ).not.toBeNull();
  });

  it("counts up the existing metrics over 900 ms when motion is allowed", async () => {
    vi.mocked(matchMedia).mockImplementation(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
    vi.spyOn(performance, "now").mockReturnValue(0);
    let next;
    vi.stubGlobal(
      "requestAnimationFrame",
      vi.fn((callback) => {
        next = callback;
        return 1;
      }),
    );
    await render("/ember", "ember");
    expect(target.querySelector(".stats").textContent).toContain("~0 ms");
    next(450);
    await tick();
    expect(target.querySelector(".stats").textContent).toContain(
      `~${Math.round(22 * 0.875)} ms`,
    );
    next(900);
    await tick();
    expect(target.querySelector(".stats").textContent).toContain(
      `${servingWakeMs} ms`,
    );
    expect(target.querySelector(".stats").textContent).toContain("~22 ms");
    expect(requestAnimationFrame).toHaveBeenCalledTimes(2);
  });

  it.each(["/ember/firecracker", "/ember/agents"])(
    "retains root-nav suppression for the separate story at %s",
    async (path) => {
      await render(path, "isolated");
      expect(target.querySelector(".md-nav, .chrome")).toBeNull();
    },
  );
});
