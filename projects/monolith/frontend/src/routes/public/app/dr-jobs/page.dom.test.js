// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { hydrate, mount, tick, unmount } from "svelte";
import Page from "./+page.svelte";
import Route from "../../../../../test/DrJobsRoute.svelte";
import Fixture from "../../../../../dr-jobs-preview/App.svelte";
import {
  fixtureData,
  jobs,
  longTown,
  longReference,
  now,
} from "../../../../../dr-jobs-preview/fixtures.js";
import { calls } from "../../../../../dr-jobs-preview/navigation.js";
import { page, setPage } from "../../../../../dr-jobs-preview/state.svelte.js";
import { withServerRender } from "../../../../../dr-jobs-preview/server-render.js";

vi.mock(
  "$app/state",
  () => import("../../../../../dr-jobs-preview/state.svelte.js"),
);
vi.mock(
  "$app/stores",
  () => import("../../../../../dr-jobs-preview/stores.js"),
);
vi.mock(
  "$app/navigation",
  () => import("../../../../../dr-jobs-preview/navigation.js"),
);

let instance;
let target;
const text = (node) => node.textContent.replace(/\s+/g, " ").trim();
const links = () => [...target.querySelectorAll("a.row")];
const control = (name) =>
  [...target.querySelectorAll(".toggle button")].find((b) =>
    text(b).startsWith(name),
  );
const settle = async () => {
  await tick();
  await tick();
};

function chooseTown(value) {
  const select = target.querySelector("select");
  const query = select.querySelector.bind(select);
  // Happy DOM 20.8.9 exposes selectedOptions but does not match option:checked.
  // Bridge only that DOM selector used by Svelte's native binding. Chromium
  // exercises the same production control without this bridge.
  const checked = vi
    .spyOn(select, "querySelector")
    .mockImplementation((selector) =>
      selector === ":checked" ? select.selectedOptions[0] : query(selector),
    );
  select.value = value;
  select.dispatchEvent(new Event("change", { bubbles: true }));
  checked.mockRestore();
}

beforeEach(() => {
  window.happyDOM.settings.disableCSSFileLoading = true;
  window.happyDOM.settings.handleDisabledFileLoadingAsSuccess = true;
  vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
  vi.setSystemTime(now);
  calls.goto.length = 0;
  calls.invalidations = 0;
  target = document.createElement("div");
  document.body.append(target);
});

afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  target.remove();
  document.documentElement.removeAttribute("data-theme");
  localStorage.clear();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

async function render(
  query = "",
  data = fixtureData(),
  component = Page,
  path = "/app/dr-jobs",
) {
  setPage(`https://preview.invalid${path}${query}`, data);
  instance = mount(component, { target, props: { data } });
  await settle();
}

describe("DR jobs production page", () => {
  it.each(["/app/dr-jobs", "/public/app/dr-jobs"])(
    "composes one main and scheme control, with no legacy navigation at %s",
    async (path) => {
      await render("", fixtureData(), Route, path);
      expect(target.querySelectorAll("main")).toHaveLength(1);
      expect(target.querySelector("main.td.sheet")).not.toBeNull();
      expect(target.querySelectorAll(".chrome, button.scheme")).toHaveLength(2);
      expect(target.querySelectorAll('nav[aria-label="Site"]')).toHaveLength(1);
      expect(target.querySelector(".md-nav")).toBeNull();
      expect(
        target.querySelector('.trail [aria-current="page"]').textContent,
      ).toBe("dr-jobs");
      expect(document.title).toBe("NHS Scotland anaesthetics consultant jobs");
      expect(
        document.head.querySelector('meta[name="description"]').content,
      ).toBe(
        "Aggregated NHS Scotland consultant and locum consultant anaesthetics vacancies, refreshed hourly from apply.jobs.scot.nhs.uk.",
      );
      expect(text(target.querySelector("h1"))).toBe(
        "NHS Scotland anaesthetics consultant vacancies",
      );
      const source = target.querySelector(".source a");
      expect(source.href).toBe("https://apply.jobs.scot.nhs.uk/Home/Job");
      expect(text(target.querySelector(".source"))).toContain("hourly from");
    },
  );

  it.each(["live", "history"])(
    "keeps %s counts, source order, titles and external link contracts",
    async (view) => {
      await render(view === "history" ? "?view=history" : "");
      const expected = jobs.filter((j) => j.is_live === (view === "live"));
      expect(links().map((a) => a.href)).toEqual(expected.map((j) => j.url));
      links().forEach((a, i) => {
        const j = expected[i];
        expect(a.target).toBe("_blank");
        expect(a.rel.split(" ")).toContain("noopener");
        expect(a.getAttribute("aria-label")).toContain(
          text(a.querySelector(".r-title")),
        );
        if (j.town) expect(a.getAttribute("aria-label")).toContain(j.town);
        expect(a.getAttribute("aria-label")).toContain(
          `closes ${text(a.querySelector(".r-date"))}`,
        );
        if (j.reference)
          expect(text(a.querySelector(".ref"))).toBe(j.reference);
      });
      expect(text(target.querySelector(".stats"))).toBe("5 live · 3 past");
      expect(text(control("Live"))).toBe("Live (5)");
      expect(text(control("Past"))).toBe("Past (3)");
      expect(
        control(view === "live" ? "Live" : "Past").getAttribute("aria-pressed"),
      ).toBe("true");
      expect(
        control(view === "live" ? "Past" : "Live").getAttribute("aria-pressed"),
      ).toBe("false");
      expect(calls.goto).toEqual([]);
    },
  );

  it("renders missing fields, title fallback and long metadata without inventing values", async () => {
    await render();
    const optional = links().find((a) => a.href.endsWith("fixture-optional"));
    expect(text(optional.querySelector(".r-meta"))).toBe("");
    expect(text(optional.querySelector(".r-date"))).toBe("n/a");
    expect(text(optional.querySelector(".r-rel"))).toBe("");
    expect(optional.querySelector(".badge")).toBeNull();
    const long = links().find((a) => a.href.endsWith("fixture-long"));
    expect(text(long)).toContain(longTown);
    expect(text(long.querySelector(".ref"))).toBe(longReference);
    expect(text(long.querySelector(".r-title"))).not.toContain(longReference);
    expect(text(links()[3].querySelector(".r-title"))).toBe("REFONLY");
    expect(text(target)).not.toMatch(/undefined|null|NaN/);
  });

  it("uses deterministic UTC closing and New boundaries, and never marks past records New", async () => {
    await render();
    expect(links().map((a) => text(a.querySelector(".r-rel")))).toEqual([
      "closes today",
      "1 day left",
      "7 days left",
      "8 days left",
      "",
    ]);
    expect(links().map((a) => !!a.querySelector(".urgent"))).toEqual([
      true,
      true,
      true,
      false,
      false,
    ]);
    expect(links().map((a) => !!a.querySelector(".badge.new"))).toEqual([
      true,
      false,
      true,
      false,
      false,
    ]);
    expect(text(links()[0].querySelector(".r-date"))).toBe("19 Oct 2026");
    control("Past").click();
    await settle();
    expect(links().map((a) => text(a.querySelector(".r-rel")))).toEqual([
      "closed",
      "closed",
      "",
    ]);
    expect(target.querySelector(".new, .urgent")).toBeNull();
  });

  it.each([
    [
      "empty-live",
      "",
      "No live anaesthetics consultant posts right now. Try History for recent listings.",
    ],
    ["empty-history", "?view=history", "No past listings recorded yet."],
  ])(
    "keeps the %s message while the other bucket remains reachable",
    async (scenario, query, message) => {
      await render(query, fixtureData(scenario));
      expect(links()).toHaveLength(0);
      expect(text(target.querySelector(".empty"))).toBe(message);
      control(scenario === "empty-live" ? "Past" : "Live").click();
      await settle();
      expect(links().length).toBeGreaterThan(0);
      expect(target.querySelector(".empty")).toBeNull();
    },
  );

  it("renders an absent listings payload as empty", async () => {
    await render("", {});
    expect(text(control("Live"))).toBe("Live (0)");
    expect(text(control("Past"))).toBe("Past (0)");
    expect(links()).toHaveLength(0);
  });

  it("initializes a selected town with sorted options and an associated label", async () => {
    await render("?town=Glasgow&campaign=retained");
    const select = target.querySelector("select");
    expect(select.value).toBe("Glasgow");
    expect(target.querySelector(`label[for="${select.id}"]`).textContent).toBe(
      "Filter by location",
    );
    expect([...select.options].map((o) => o.textContent)).toEqual([
      "All locations",
      "Aberdeen",
      "Glasgow",
      longTown,
    ]);
    expect(links()).toHaveLength(2);
    expect(text(target.querySelector(".stats"))).toBe("5 live · 3 past");
    expect(calls.goto).toEqual([]);
  });

  it.each(["?town=Unknown", "?view=history&town=Unknown", "?view=invalid"])(
    "normalizes %s once, preserving unrelated params and preventing goto loops",
    async (query) => {
      await render(`${query}&campaign=retained#jobs`);
      expect(target.querySelector("select").value).toBe("");
      expect(page.url.searchParams.has("town")).toBe(false);
      expect(page.url.searchParams.get("campaign")).toBe("retained");
      expect(page.url.hash).toBe("#jobs");
      expect(calls.goto).toHaveLength(1);
      expect(calls.goto[0].options).toEqual({
        keepFocus: true,
        noScroll: true,
        replaceState: true,
      });
      setPage(page.url);
      await settle();
      expect(calls.goto).toHaveLength(1);
    },
  );

  it("writes selection changes, resets a town absent from the next bucket and keeps a shared town", async () => {
    await render("?campaign=retained");
    const select = target.querySelector("select");
    chooseTown("Glasgow");
    await settle();
    expect(page.url.searchParams.get("town")).toBe("Glasgow");
    expect(links()).toHaveLength(2);
    control("Past").click();
    await settle();
    expect(select.value).toBe("");
    expect(page.url.searchParams.has("town")).toBe(false);
    expect(page.url.searchParams.get("view")).toBe("history");
    chooseTown(longTown);
    await settle();
    control("Live").click();
    await settle();
    expect(select.value).toBe(longTown);
    expect(page.url.searchParams.get("town")).toBe(longTown);
    expect(page.url.searchParams.has("view")).toBe(false);
    expect(page.url.searchParams.get("campaign")).toBe("retained");
    expect(links()).toHaveLength(1);
    expect(calls.goto).toHaveLength(4);
  });

  it("invalidates only every 30 minutes and clears the interval on unmount", async () => {
    await render();
    expect(vi.getTimerCount()).toBe(1);
    vi.advanceTimersByTime(30 * 60_000 - 1);
    expect(calls.invalidations).toBe(0);
    vi.advanceTimersByTime(1);
    expect(calls.invalidations).toBe(1);
    vi.advanceTimersByTime(30 * 60_000);
    expect(calls.invalidations).toBe(2);
    await unmount(instance);
    instance = undefined;
    expect(vi.getTimerCount()).toBe(0);
    vi.advanceTimersByTime(30 * 60_000);
    expect(calls.invalidations).toBe(2);
  });

  it("persists both scheme choices with accessible action labels", async () => {
    await render();
    const button = target.querySelector("button.scheme");
    button.click();
    await settle();
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(button.getAttribute("aria-label")).toBe("Switch to day scheme");
    expect(localStorage.getItem("td-theme")).toBe("dark");
    button.click();
    await settle();
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(button.getAttribute("aria-label")).toBe("Switch to night scheme");
  });

  it("renders populated and empty buckets on the server before client effects run", async () => {
    await withServerRender((renderFixture) => {
      for (const [scenario, view, count] of [
        ["populated", "live", 5],
        ["populated", "history", 3],
        ["empty-live", "live", 0],
        ["empty-history", "history", 0],
      ]) {
        const rendered = renderFixture({
          data: fixtureData(scenario),
          url: `https://preview.invalid/app/dr-jobs?view=${view}`,
        });
        target.innerHTML = rendered.body;
        expect(links()).toHaveLength(count);
        expect(
          control(view === "live" ? "Live" : "Past").getAttribute(
            "aria-pressed",
          ),
        ).toBe("true");
        expect(!!target.querySelector(".empty")).toBe(count === 0);
        expect(rendered.head).toContain(
          "refreshed hourly from apply.jobs.scot.nhs.uk.",
        );
        if (count) expect(text(target)).toContain(longTown);
      }
    });
    expect(calls.goto).toEqual([]);
    expect(calls.invalidations).toBe(0);
  }, 20000);

  it("hydrates actual SSR markup without replacing job links or emitting warnings", async () => {
    const props = {
      data: fixtureData(),
      url: "https://preview.invalid/app/dr-jobs?view=history&town=Edinburgh",
    };
    const rendered = await withServerRender((renderFixture) =>
      renderFixture(props),
    );
    expect(rendered.head).toContain(
      "NHS Scotland anaesthetics consultant jobs",
    );
    target.innerHTML = rendered.body;
    const original = target.querySelector("a.row");
    expect(original.href).toContain("fixture-closed");
    expect(links()).toHaveLength(1);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    instance = hydrate(Fixture, { target, props, recover: false });
    await settle();
    expect(target.querySelector("a.row")).toBe(original);
    expect(target.querySelector("select").value).toBe("Edinburgh");
    expect(calls.goto).toEqual([]);
    expect(warn.mock.calls).toEqual([]);
    expect(error.mock.calls).toEqual([]);
    control("Live").click();
    await settle();
    expect(links()).toHaveLength(5);
  }, 20000);
});
