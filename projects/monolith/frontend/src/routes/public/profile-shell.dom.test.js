// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Route from "../../../test/ProfileRoute.svelte";
import { setPage } from "../../../test/profile-page.svelte.js";
import { reroute } from "../../hooks.js";
import * as cv from "./cv/cv-data.js";

vi.mock("$app/state", () => import("../../../test/profile-page.svelte.js"));
vi.mock("$app/stores", () => import("../../../test/profile-stores.js"));

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
  document.documentElement.removeAttribute("data-theme");
  localStorage.clear();
});
async function render(path, surface, host = "jomcgi.dev", data = {}) {
  setPage(
    `https://${host}${path}`,
    { hasBlog: true, ...data },
    surface === "error" ? { message: "missing" } : null,
  );
  instance = mount(Route, {
    target,
    props: { surface, data: { stats: null, ...data } },
  });
  await tick();
}

describe("home/CV composed route chrome", () => {
  it.each([
    ["/", "home", true],
    ["/", "home", false],
    ["/cv", "cv", true],
    ["/cv", "cv", false],
  ])(
    "keeps %s (%s) footer Blog navigation conditional on hasBlog=%s",
    async (path, surface, hasBlog) => {
      await render(path, surface, "jomcgi.dev", { hasBlog });
      const navigation = target.querySelector(
        'nav[aria-label="Continue reading"]',
      );
      expect(navigation).not.toBeNull();
      expect(navigation.querySelectorAll('a[href="/blog"]')).toHaveLength(
        hasBlog ? 1 : 0,
      );
    },
  );

  it.each([
    ["/", "home"],
    ["/public/", "home"],
    ["/cv", "cv"],
    ["/cv/", "cv"],
    ["/public/cv", "cv"],
  ])("renders one chrome and scheme control at %s", async (path, surface) => {
    await render(path, surface);
    expect(target.querySelectorAll(".chrome")).toHaveLength(1);
    expect(target.querySelectorAll("button.scheme")).toHaveLength(1);
    expect(target.querySelector(".md-nav")).toBeNull();
    expect(target.querySelectorAll("main")).toHaveLength(1);
    expect(target.querySelectorAll('nav[aria-label="Site"]')).toHaveLength(1);
    expect(
      target.querySelector('nav[aria-label="Continue reading"] a[href="/cv"]'),
    ).not.toBeNull();
    expect(
      document.head.querySelector('script[type="application/ld+json"]')
        .textContent,
    ).toContain('"Person"');
    const toggle = target.querySelector("button.scheme");
    toggle.click();
    await tick();
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("td-theme")).toBe("dark");
    expect(toggle.getAttribute("aria-label")).toBe("Switch to day scheme");
  });

  it.each(["private.jomcgi.dev", "friends.jomcgi.dev"])(
    "keeps %s paths in their own tier without injecting public chrome",
    async (host) => {
      for (const path of ["/", "/cv"]) {
        expect(reroute({ url: new URL(`https://${host}${path}`) })).toBe(
          `/${host.split(".")[0]}${path}`,
        );
        await render(path, "isolated", host);
        expect(
          target.querySelector(".md-nav, .chrome, .scheme, .public-theme"),
        ).toBeNull();
        await unmount(instance);
        instance = undefined;
      }
    },
  );

  it.each(["/cvstore", "/docstore", "/public/cv/unknown"])(
    "does not suppress legacy navigation on the unrelated route %s",
    async (path) => {
      await render(path, "legacy");
      expect(target.querySelectorAll(".md-nav")).toHaveLength(1);
      expect(target.querySelector(".chrome, .scheme")).toBeNull();
    },
  );

  it.each([
    "/artifact/one",
    "/public/artifact/one",
    "/app/ships",
    "/private/tools",
    "/friends/register",
  ])("preserves full-bleed/tier suppression for %s", async (path) => {
    await render(
      path,
      "isolated",
      path.startsWith("/friends") ? "friends.jomcgi.dev" : "jomcgi.dev",
    );
    expect(target.querySelector(".md-nav, .chrome")).toBeNull();
  });

  it.each([
    ["/", "home"],
    ["/cv", "cv"],
  ])("keeps the maintenance notice on %s", async (path, surface) => {
    await render(path, surface, "jomcgi.dev", {
      maintenanceBanner: "Fixture maintenance notice",
    });
    expect(
      target.querySelector(".maintenance-banner[role=status]").textContent,
    ).toBe("Fixture maintenance notice");
  });

  it("preserves the slop notice exception and root error chrome", async () => {
    await render("/slop/factory", "legacy", "jomcgi.dev", {
      maintenanceBanner: "notice",
    });
    expect(target.querySelector(".maintenance-banner, .md-nav")).toBeNull();
    await unmount(instance);
    await render("/missing", "error");
    expect(target.querySelectorAll(".chrome")).toHaveLength(1);
    expect(target.querySelector(".md-nav, .public-theme")).toBeNull();
    expect(target.querySelector("h1").textContent).toContain("off the map");
  });
});

describe("CV content and links", () => {
  it("renders every canonical CV record, emphasis and link without markdown leakage", async () => {
    await render("/cv", "cv");
    const text = target.querySelector("main").textContent.replace(/\s+/g, " ");
    const plain = (value) =>
      value
        .replace(/\*\*(.+?)\*\*/g, "$1")
        .replace(/\[(.+?)\]\((.+?)\)/g, "$1")
        .replace(/\s+/g, " ");
    const values = [
      cv.name,
      cv.tagline,
      cv.summary,
      cv.earlierCareer,
      cv.personalIntro,
      ...cv.projects,
    ];
    for (const job of cv.jobs) {
      values.push(
        job.company,
        job.title,
        job.dates,
        job.blurb ?? "",
        ...(job.bullets ?? []),
      );
      for (const h of job.highlights ?? [])
        values.push(h.title, h.intro ?? "", ...h.bullets);
    }
    for (const category of cv.skills)
      values.push(category.label, ...category.items);
    for (const value of values) {
      expect(text).toContain(plain(value));
      for (const [, label, href] of value.matchAll(/\[(.+?)\]\((.+?)\)/g)) {
        const link = [...target.querySelectorAll("a")].find(
          (a) => a.getAttribute("href") === href && a.textContent === label,
        );
        expect(link, href).toBeDefined();
        expect(link.getAttribute("rel")).toBe("noreferrer");
      }
    }
    expect(target.querySelector("h1").textContent).toBe(cv.name);
    expect(target.querySelectorAll("#experience article")).toHaveLength(
      cv.jobs.length,
    );
    expect(target.querySelector("strong")).not.toBeNull();
    expect(text).not.toContain("**");
    for (const href of [
      `mailto:${cv.contact.email}`,
      cv.contact.github.href,
      cv.contact.linkedin.href,
    ])
      expect(target.querySelector(`a[href="${href}"]`)).not.toBeNull();
    for (const section of ["experience", "personal", "expertise"])
      expect(target.querySelector(`#${section} h2`)).not.toBeNull();
  });
});
