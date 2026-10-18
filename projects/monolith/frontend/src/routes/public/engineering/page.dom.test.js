// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Page from "./+page.svelte";
import { projects } from "./engineering-data.js";

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

async function render() {
  instance = mount(Page, { target });
  await tick();
  return target;
}

describe("engineering page", () => {
  it("is one technical-drawing sheet with every system as a numbered record", async () => {
    const root = await render();
    expect(root.querySelector("main.td.sheet")).not.toBeNull();

    const records = [...root.querySelectorAll("article.edition.system")];
    expect(records).toHaveLength(projects.length);
    records.forEach((record, i) => {
      const project = projects[i];
      expect(record.id).toBe(project.id);
      expect(record.querySelector(".ed-head .num").textContent).toBe(
        String(i + 1).padStart(2, "0"),
      );
      expect(record.querySelector(".ed-lead h2").textContent).toBe(
        project.title,
      );
      expect(record.querySelectorAll(".spec .row")).toHaveLength(
        project.facts.length,
      );
      expect(record.querySelector("figure.fig figcaption")).not.toBeNull();
    });
  });

  it("indexes every record in the spine, in order, by its anchor", async () => {
    const root = await render();
    const links = [
      ...root.querySelectorAll('.spine .index[aria-label="Systems"] a'),
    ];
    expect(links.map((a) => a.getAttribute("href"))).toEqual(
      projects.map((p) => `#${p.id}`),
    );
    expect(links[0].querySelector(".num").textContent).toBe("01");
  });

  it("names the source and live destinations in the record head band", async () => {
    const root = await render();
    const links = (id) =>
      [...root.querySelectorAll(`#${id} .ed-head a`)].map((a) => ({
        href: a.getAttribute("href"),
        target: a.getAttribute("target"),
      }));
    expect(
      links("agent-platform").some(
        (l) => l.href.includes("github.com") && l.target === "_blank",
      ),
    ).toBe(true);
    expect(
      links("trips").some((l) => l.href === "/app/trips" && l.target === null),
    ).toBe(true);
  });

  it("carries the site sections and the stack row, and none of the retired chrome", async () => {
    const root = await render();
    expect(
      root.querySelector('.spine nav[aria-label="Site"] a[href="/docs"]'),
    ).not.toBeNull();
    expect(root.querySelectorAll(".intro .divided li").length).toBeGreaterThan(
      5,
    );
    for (const retired of [
      ".btn",
      ".card-hard",
      ".display",
      ".eyebrow",
      ".marquee",
      ".rail",
      ".sticker",
    ]) {
      expect(root.querySelector(retired), retired).toBeNull();
    }
  });
});
