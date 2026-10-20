// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Homepage from "./+page.svelte";
import { apps } from "$lib/public/apps.js";
import {
  STATS_REFRESH_INTERVAL_MS,
  STATS_AGE_TICK_INTERVAL_MS,
} from "./homepage-stats.js";

let instance;
let target;

const posts = [
  {
    slug: "ember-conformance",
    title: "Formal verification at home",
    date: "2026-10-02",
    summary:
      "Trace-based conformance testing for EmberVM, replayed from one real run.",
  },
  {
    slug: "125b-on-a-4090",
    title: "How to serve 134 GB of model weights with 24 GB VRAM / 64 GB RAM",
    date: "2026-09-01",
    summary: "How oom-inference serves roughly 134 GB of text-model weights.",
  },
];

function hover(el, pointerType = "mouse") {
  el.dispatchEvent(new PointerEvent("pointerenter", { pointerType }));
}

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
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("homepage unit", () => {
  it("leads with the slop factory key and keys every app and section", async () => {
    instance = mount(Homepage, {
      target,
      props: { data: { stats: null, posts } },
    });
    await tick();

    const keys = target.querySelectorAll('nav[aria-label="Site"] a.key');
    expect(keys).toHaveLength(9);
    const [primary] = keys;
    expect(primary.classList.contains("go")).toBe(true);
    expect(primary.getAttribute("href")).toBe("/slop/factory");
    expect(primary.getAttribute("aria-label")).toBe("Slop factory");
    expect(primary.querySelector(".legend").textContent).toBe(
      "software factory",
    );
    for (const href of ["/cv", "/docs"]) {
      expect(
        target.querySelector(`nav[aria-label="Site"] a[href="${href}"]`),
      ).not.toBeNull();
    }
    for (const slug of [
      "notes",
      "grimoire",
      "ships",
      "stars",
      "hikes",
      "trips",
    ]) {
      const app = apps.find((a) => a.slug === slug);
      expect(
        target.querySelector(`nav[aria-label="Site"] a[href="${app.href}"]`),
      ).not.toBeNull();
    }
    expect(target.querySelectorAll("h1")).toHaveLength(1);
    expect(target.querySelector("h1").textContent).toContain(
      "Joe McGinley, Scotland.",
    );
    expect(target.querySelector(".glass .small").textContent).toContain(
      "Semgrep",
    );
  });

  it("reads the hovered key out in the glass and returns to the name, mouse only", async () => {
    instance = mount(Homepage, {
      target,
      props: { data: { stats: null, posts } },
    });
    await tick();
    const pad = target.querySelector('nav[aria-label="Site"]');
    const cv = pad.querySelector('a[href="/cv"]');

    hover(cv, "touch");
    await tick();
    expect(target.querySelector(".glass p.big")).toBeNull();

    hover(cv);
    await tick();
    expect(target.querySelector(".glass p.big").textContent).toContain("CV");
    expect(target.querySelector(".glass .small").textContent).toContain(
      "underwriting",
    );
    expect(target.querySelector("h1").classList.contains("sr-only")).toBe(true);

    hover(pad.querySelector(".go"));
    await tick();
    const big = target.querySelector(".glass p.big");
    expect(big.querySelector(".sw").textContent).toBe("Software");
    expect(big.textContent.replace(/ /g, " ")).toContain("Slop factory");

    pad.dispatchEvent(new PointerEvent("pointerleave"));
    await tick();
    expect(target.querySelector(".glass p.big")).toBeNull();
    expect(target.querySelector("h1").classList.contains("sr-only")).toBe(
      false,
    );
  });

  it("lists the latest posts as legends and reads them out on hover", async () => {
    instance = mount(Homepage, {
      target,
      props: { data: { stats: null, posts } },
    });
    await tick();
    const links = target.querySelectorAll('section[aria-label="Writing"] a');
    expect(links).toHaveLength(2);
    expect(links[0].getAttribute("href")).toBe("/blog/ember-conformance");
    expect(links[0].textContent).toContain("Formal verification at home");
    hover(links[1]);
    await tick();
    expect(target.querySelector(".glass p.big").textContent).toContain(
      "134 GB",
    );
    expect(target.querySelector(".glass .small").textContent).toContain(
      "oom-inference",
    );
  });

  it("omits the writing legends when nothing is published", async () => {
    instance = mount(Homepage, {
      target,
      props: { data: { stats: null, posts: [] } },
    });
    await tick();
    expect(target.querySelector('section[aria-label="Writing"]')).toBeNull();
    expect(target.querySelector(".deploy")).toBeNull();
    expect(target.querySelector(".led").textContent).toContain("live");
  });

  it("keeps the deployment readout live and aborts the poll on unmount", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-18T12:00:00Z"));
    const stats = {
      deploy: {
        latest_commit_sha: "old1234567",
        deployed_at: "2026-10-18T11:50:00Z",
      },
    };
    const fetchFn = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          deploy: {
            latest_commit_sha: "new7654321",
            deployed_at: "2026-10-18T12:00:00Z",
          },
        }),
      })
      .mockImplementation(() => new Promise(() => {}));
    vi.stubGlobal("fetch", fetchFn);
    instance = mount(Homepage, {
      target,
      props: { data: { stats, posts: [] } },
    });
    await tick();
    expect(target.querySelector(".deploy").textContent).toBe(
      "deployed 10m ago",
    );
    expect(target.querySelector(".led").textContent).toContain("old1234");
    await vi.advanceTimersByTimeAsync(STATS_REFRESH_INTERVAL_MS);
    expect(target.querySelector(".deploy").textContent).toBe("deployed 5m ago");
    expect(target.querySelector(".led").textContent).toContain("new7654");
    await vi.advanceTimersByTimeAsync(STATS_AGE_TICK_INTERVAL_MS * 10);
    expect(target.querySelector(".deploy").textContent).toBe(
      "deployed 10m ago",
    );
    await vi.advanceTimersByTimeAsync(STATS_REFRESH_INTERVAL_MS);
    const signal = fetchFn.mock.calls.at(-1)[1].signal;
    await unmount(instance);
    instance = undefined;
    expect(signal.aborted).toBe(true);
  });
});
