// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Homepage from "./+page.svelte";
import {
  STATS_REFRESH_INTERVAL_MS,
  STATS_AGE_TICK_INTERVAL_MS,
} from "./homepage-stats.js";

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
  vi.useRealTimers();
});

describe("homepage calls to action", () => {
  it("leads with Slop Factory and keeps the homelab as the secondary destination", async () => {
    instance = mount(Homepage, { target, props: { data: { stats: null } } });
    await tick();

    const links = target.querySelectorAll(".hero-cta-row a");
    expect(links).toHaveLength(2);
    const [primary, secondary] = links;
    expect(primary.classList.contains("factory-cta")).toBe(true);
    expect(primary.getAttribute("href")).toBe("/slop/factory");
    expect(primary.getAttribute("aria-label")).toBe("Slop Factory");
    expect(primary.textContent).toContain("Slop Factory");
    expect(primary.querySelector("s")?.textContent).toBe("Software");
    expect(primary.querySelector("s")?.getAttribute("aria-hidden")).toBe(
      "true",
    );
    expect(secondary.classList.contains("factory-cta")).toBe(false);
    expect(secondary.getAttribute("href")).toBe("#homelab");
    expect(target.querySelector("#homelab")).not.toBeNull();
  });

  it("renders empty stats without fabricated figures and preserves hardware and map links", async () => {
    instance = mount(Homepage, { target, props: { data: { stats: null } } });
    await tick();
    expect(target.querySelector('[role="status"]').textContent).toContain(
      "Stats unavailable",
    );
    expect(target.querySelector('ul[aria-label="Homelab stats"]')).toBeNull();
    expect(
      target.querySelectorAll('dl[aria-label="Hardware"] dt'),
    ).toHaveLength(3);
    for (const path of [
      "ships",
      "stars",
      "hikes",
      "trips",
      "notes",
      "grimoire",
    ]) {
      expect(
        target.querySelector(`#homelab a[href="/app/${path}"]`),
      ).not.toBeNull();
    }
    expect(target.querySelector("h1").textContent).toContain(
      "underwriting policies",
    );
    expect(target.querySelectorAll("h1")).toHaveLength(1);
  });

  it("retains stale stats, refreshes both readouts, ticks age and aborts on unmount", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-18T12:00:00Z"));
    const stats = {
      cluster: { nodes: 2, pods: 42 },
      deploy: { latest_commit_sha: "old", deployed_at: "2026-10-18T11:50:00Z" },
    };
    const fetchFn = vi
      .fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          cluster: { nodes: 3, pods: 50 },
          deploy: {
            latest_commit_sha: "new",
            deployed_at: "2026-10-18T12:00:00Z",
          },
        }),
      })
      .mockImplementation(() => new Promise(() => {}));
    vi.stubGlobal("fetch", fetchFn);
    instance = mount(Homepage, { target, props: { data: { stats } } });
    await tick();
    await vi.advanceTimersByTimeAsync(STATS_REFRESH_INTERVAL_MS);
    expect(target.textContent).toContain("2 nodes · 42 pods");
    expect(target.textContent).toContain("deployed 15m ago");
    await vi.advanceTimersByTimeAsync(STATS_REFRESH_INTERVAL_MS);
    expect(target.textContent).toContain("3 nodes · 50 pods");
    expect(target.querySelector("#homelab .ed-head").textContent).toBe(
      "3 nodes",
    );
    expect(target.textContent).toContain("last commit: new");
    await vi.advanceTimersByTimeAsync(STATS_AGE_TICK_INTERVAL_MS);
    expect(target.textContent).toContain("deployed 11m ago");
    await vi.advanceTimersByTimeAsync(STATS_REFRESH_INTERVAL_MS);
    const signal = fetchFn.mock.calls.at(-1)[1].signal;
    await unmount(instance);
    instance = undefined;
    expect(signal.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });
});
