import { fireEvent, render } from "@testing-library/react";
import { act } from "react";
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HomePage } from "./home-page";
import { HOME_KEYS, sectionHref } from "./home-data";
import { HOME, hrefFor, SECTIONS } from "./sections";
import { sitemap } from "./sitemap";
import { PAGES } from "./pages";
import { Root } from "./root";
import { hub, latestPosts, posts } from "../site-inputs.mjs";
import { llms, robots } from "../machine-readable.mjs";

const portrait = vi.hoisted(() => ({
  create: vi.fn(() => ({ destroy: vi.fn() })),
}));
vi.mock("./home-portrait.js", () => ({ createPortrait: portrait.create }));
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  portrait.create.mockClear();
  localStorage.clear();
  delete document.documentElement.dataset.homeScheme;
});
const mount = () =>
  render(
    <Root>
      <HomePage />
    </Root>,
  );

describe("home port", () => {
  it("selects the newest two posts even when the manifest is unsorted", () => {
    const fixture = ["2025-01-01", "2026-01-01", "2025-06-01"].map((date) => ({
      slug: date,
      title: date,
      summary: date,
      date,
    }));
    expect(latestPosts(fixture).map((post) => post.date)).toEqual([
      "2026-01-01",
      "2025-06-01",
    ]);
  });

  it("keeps the original readout text and follows each section's migration state", () => {
    const source = readFileSync(
      new URL("src/routes/public/+page.svelte", hub),
      "utf8",
    );
    for (const key of HOME_KEYS) {
      expect(source).toContain(key.small);
      const section = SECTIONS.find((section) => section.name === key.section)!;
      expect(sectionHref(key.section)).toBe(hrefFor(section));
      const before = section.live;
      try {
        section.live = !before;
        expect(sectionHref(key.section)).toBe(hrefFor(section));
      } finally {
        section.live = before;
      }
    }
    expect(HOME.live).toBe(true);
    expect(hrefFor(HOME)).toBe("/");
  });

  it.each([
    ["2026-01-15T12:34:56Z", "12:34:56", "12:34:57"],
    ["2026-07-15T12:34:56Z", "13:34:56", "13:34:57"],
  ])(
    "keeps the London clock correct across daylight saving at %s",
    (now, london, nextSecond) => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date(now));
      const { container } = mount();
      expect(container.querySelector(".meta b")?.textContent).toBe(london);
      act(() => vi.advanceTimersByTime(1000));
      expect(container.querySelector(".meta b")?.textContent).toBe(nextSecond);
    },
  );

  it.each(["pointerMove", "pointerDown", "keyDown"] as const)(
    "loads the portrait on %s once and destroys it on unmount",
    async (intent) => {
      const view = mount();
      await vi.dynamicImportSettled();
      expect(portrait.create).not.toHaveBeenCalled();
      fireEvent[intent](window, { key: "Tab" });
      await vi.dynamicImportSettled();
      expect(portrait.create).toHaveBeenCalledTimes(1);
      fireEvent.pointerMove(window);
      fireEvent.pointerDown(window);
      fireEvent.keyDown(window, { key: "Tab" });
      await vi.dynamicImportSettled();
      expect(portrait.create).toHaveBeenCalledTimes(1);
      const engine = portrait.create.mock.results[0]!.value;
      view.unmount();
      expect(engine.destroy).toHaveBeenCalledTimes(1);
      fireEvent[intent](window, { key: "Tab" });
      await vi.dynamicImportSettled();
      expect(portrait.create).toHaveBeenCalledTimes(1);
    },
  );
});

describe("build-time machine-readable files", () => {
  it("allows the public site, disallows slop and advertises the canonical sitemap", () => {
    expect(robots()).toBe(
      "User-agent: *\nAllow: /\nDisallow: /slop\n\nSitemap: https://jomcgi.dev/sitemap.xml\n",
    );
  });
  it("uses the shared posts in sitemap and LLM digest, omitting retired notes", () => {
    const xml = sitemap(Object.keys(PAGES));
    const digest = llms(posts);
    for (const post of posts) {
      expect(xml).toContain(`<loc>https://jomcgi.dev/blog/${post.slug}</loc>`);
      expect(digest).toContain(
        `- [${post.title}](https://jomcgi.dev/blog/${post.slug}): ${post.summary}`,
      );
    }
    expect(xml).not.toContain("/app/notes");
    expect(digest).not.toContain("/app/notes");
    expect(digest).toContain("Location: Scotland, United Kingdom");
    expect(digest).toContain("https://scotland-mcp.jomcgi.dev/mcp");
    expect(llms([])).not.toContain("## Blog");
  });
});

it("lists every live public page and excludes other entries", () => {
  const xml = sitemap(Object.keys(PAGES));
  const locations = new Set(
    Array.from(xml.matchAll(/<loc>(.*?)<\/loc>/g), ([, url]) => url),
  );
  for (const section of [HOME, ...SECTIONS]) {
    expect(
      locations.has("https://jomcgi.dev" + section.path),
      section.path,
    ).toBe(section.live && section.listed !== false);
  }
  for (const entry of Object.keys(PAGES)) {
    const path = ("/" + entry)
      .replace(/\/index\.html$/, "/")
      .replace(/\.html$/, "");
    const section =
      path === "/"
        ? HOME
        : SECTIONS.find((s) => path.split("/")[1] === s.path.slice(1));
    expect(locations.has("https://jomcgi.dev" + path), entry).toBe(
      section?.live === true && section.listed !== false,
    );
  }
});

it("keeps page canonicals off redirect sources", () => {
  const rules = readFileSync("public/_redirects", "utf8")
    .trim()
    .split("\n")
    .map((line) => line.trim().split(/\s+/))
    .filter(
      ([source = "", destination, status = "302"]) =>
        source.startsWith("/") && destination && /^3\d\d!?$/.test(status),
    );
  for (const entry of Object.keys(PAGES)) {
    const doc = new DOMParser().parseFromString(
      readFileSync(entry, "utf8"),
      "text/html",
    );
    for (const link of doc.querySelectorAll('link[rel="canonical"]')) {
      const path = new URL(link.getAttribute("href")!, "https://jomcgi.dev")
        .pathname;
      for (const [source = ""] of rules) {
        expect(
          source.endsWith("*")
            ? path.startsWith(source.slice(0, -1))
            : path === source,
          entry + " canonical " + path,
        ).toBe(false);
      }
    }
  }
});
