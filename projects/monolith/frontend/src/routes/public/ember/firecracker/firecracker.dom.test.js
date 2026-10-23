// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Route from "../../../../../test/ProfileRoute.svelte";
import { setPage } from "../../../../../test/profile-page.svelte.js";
import { cold, restores } from "$lib/public/fcstory/data/trace.js";
import { sandboxRestoreMs } from "$lib/public/fcstory/metrics.js";

vi.mock(
  "$app/state",
  () => import("../../../../../test/profile-page.svelte.js"),
);
vi.mock("$app/stores", () => import("../../../../../test/profile-stores.js"));

let instance, target, reduced, time, frames, nextFrame;
beforeEach(() => {
  window.happyDOM.settings.disableCSSFileLoading = true;
  window.happyDOM.settings.handleDisabledFileLoadingAsSuccess = true;
  reduced = false;
  time = 0;
  frames = new Map();
  nextFrame = 0;
  vi.spyOn(performance, "now").mockImplementation(() => time);
  vi.stubGlobal("requestAnimationFrame", (callback) => {
    frames.set(++nextFrame, callback);
    return nextFrame;
  });
  vi.stubGlobal("cancelAnimationFrame", (id) => frames.delete(id));
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    matches: query.includes("reduced-motion") && reduced,
    addEventListener() {},
    removeEventListener() {},
  }));
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
async function render(
  path = "/ember/firecracker?source=cv#replay",
  surface = "firecracker",
) {
  setPage(`https://jomcgi.dev${path}`, { hasBlog: true });
  instance = mount(Route, {
    target,
    props: { surface, data: { hasBlog: true } },
  });
  await tick();
}
async function advance(ms) {
  time += ms;
  const pending = [...frames.values()];
  frames.clear();
  for (const callback of pending) callback(time);
  await tick();
}
const text = () => target.textContent.replace(/\s+/g, " ");
const button = () => target.querySelector(".console button");

describe("Firecracker production route composition", () => {
  it.each([
    "/ember/firecracker",
    "/public/ember/firecracker",
    "/ember/firecracker?source=cv#replay",
  ])(
    "renders one sheet, accessible chrome and the full story at %s",
    async (path) => {
      const fetch = vi.spyOn(window, "fetch");
      await render(path);
      expect(target.querySelectorAll("main.td.sheet.fcstory")).toHaveLength(1);
      expect(target.querySelectorAll(".chrome")).toHaveLength(1);
      expect(target.querySelectorAll("button.scheme")).toHaveLength(1);
      expect(target.querySelector(".md-nav")).toBeNull();
      expect(target.querySelectorAll("h1")).toHaveLength(1);
      expect(target.querySelector('nav[aria-label="Site"]')).not.toBeNull();
      expect(
        target.querySelector(
          'nav[aria-label="On this page"] a[href="#replay"]',
        ),
      ).not.toBeNull();
      for (const id of ["cold", "freeze", "restore", "runs", "replay"]) {
        const section = target.querySelector(`#${id}`);
        expect(
          target.querySelector(`#${section.getAttribute("aria-labelledby")}`),
        ).not.toBeNull();
      }
      expect(text()).toContain("Boot once. Restore forever.");
      expect(text()).toContain(
        "Every page of guest RAM and every device register is written to disk",
      );
      expect(text()).toContain(
        "The bytes map from disk straight back into memory",
      );
      expect(text()).toContain("And again. And again.");
      expect(text()).toContain(`${Math.round(sandboxRestoreMs)} milliseconds`);
      expect(text()).toContain(`${cold.total.toLocaleString()} ms`);
      for (const phase of cold.phases)
        expect(text()).toContain(`${phase.ms.toFixed(1)} ms`);
      expect(target.querySelectorAll(".recorded-runs li")).toHaveLength(
        restores.length,
      );
      for (const run of restores)
        expect(text()).toContain(`${run.total.toFixed(1)} ms`);
      expect(
        target.querySelectorAll('[aria-label="Warm phases"] .row'),
      ).toHaveLength(restores[0].phases.length);
      expect(document.title).toBe(
        "Boot once, restore forever · Firecracker microVMs · jomcgi.dev",
      );
      expect(
        document.head.querySelector('meta[name="description"]').content,
      ).toContain("about 22 milliseconds");
      expect(fetch).not.toHaveBeenCalled();
      expect(document.body.style.overflow).toBe("");
      expect(frames.size).toBe(0);
    },
  );

  it("retains legacy navigation on unrelated Ember-like paths", async () => {
    await render("/emberstore", "legacy");
    expect(target.querySelectorAll(".md-nav")).toHaveLength(1);
  });

  it("keeps all explanation in the DOM under reduced motion and after resizing", async () => {
    reduced = true;
    await render();
    const before = text();
    window.dispatchEvent(new Event("resize"));
    window.dispatchEvent(new Event("scroll"));
    await tick();
    expect(text()).toBe(before);
    expect(target.querySelectorAll(".edition")).toHaveLength(6);
    expect(target.querySelector(".scroller, .static-story")).toBeNull();
    expect(frames.size).toBe(0);
  });
});

describe("recorded replay", () => {
  it("cycles every baked run, includes zero-duration phases, and wraps", async () => {
    reduced = true;
    await render();
    for (let i = 0; i <= restores.length; i++) {
      button().click();
      await tick();
      const run = restores[i % restores.length];
      expect(target.querySelector(".replay-readout").textContent).toContain(
        `run ${(i % restores.length) + 1} of ${restores.length}`,
      );
      expect(target.querySelector(".final").textContent).toBe(
        `${run.total.toFixed(1)} ms`,
      );
      const rows = [...target.querySelectorAll(".phase-table tr")];
      expect(rows).toHaveLength(run.phases.length);
      run.phases.forEach((phase, index) => {
        expect(rows[index].querySelector("th").textContent).toContain(
          phase.name,
        );
        expect(rows[index].querySelector("td").textContent).toBe(
          `${phase.ms.toFixed(1)} ms`,
        );
      });
      expect(target.querySelector(".counter b").textContent).toBe(
        String(i + 1),
      );
      expect(button().disabled).toBe(false);
    }
    expect(frames.size).toBe(0);
  });

  it.each([1, 40])(
    "honors %sx timing and rejects duplicate busy clicks",
    async (multiplier) => {
      await render();
      if (multiplier === 40) {
        target.querySelector('input[type="checkbox"]').click();
        await tick();
      }
      button().click();
      await tick();
      expect(button().disabled).toBe(true);
      expect(button().getAttribute("aria-busy")).toBe("true");
      expect(target.querySelector("input").disabled).toBe(true);
      button().click();
      await advance(restores[0].total * multiplier * 0.5);
      expect(target.querySelector(".replay-fill").style.transform).toBe(
        "scaleX(0.5)",
      );
      expect(target.querySelector(".counter b").textContent).toBe("0");
      expect(target.querySelector(".phase-table")).toBeNull();
      await advance(restores[0].total * multiplier * 0.5);
      expect(button().disabled).toBe(false);
      expect(target.querySelector(".counter b").textContent).toBe("1");
      expect(target.querySelector(".final").textContent).toBe(
        `${restores[0].total.toFixed(1)} ms`,
      );
      if (multiplier === 40) expect(text()).toContain("40× slower");
    },
  );

  it("reads changed motion preferences for the next replay", async () => {
    await render();
    reduced = true;
    button().click();
    await tick();
    expect(target.querySelector(".counter b").textContent).toBe("1");
    expect(frames.size).toBe(0);
  });

  it("cancels the outstanding animation on navigation/unmount", async () => {
    await render();
    button().click();
    await tick();
    expect(frames.size).toBe(1);
    await unmount(instance);
    instance = undefined;
    expect(frames.size).toBe(0);
    await advance(10000);
    expect(target.textContent).toBe("");
  });
});
