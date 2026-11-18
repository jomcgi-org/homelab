import { readFileSync } from "node:fs";
import { act } from "react";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { categories, intro, marqueeItems, projects } from "./engineering-data";
import { EngineeringPage } from "./engineering-page";
import { diagrams } from "./diagrams";
import { render as prerender } from "./prerender";
import { Root } from "./root";
import { hrefFor, SECTIONS } from "./sections";

// These are the hub diagram captions, in the same order as its roster.
const CAPTIONS = {
  "agent-platform": "Agent platform",
  embervm: "EmberVM (serving requests bypass the control plane)",
  "knowledge-graph": "Knowledge pipeline",
  loom: "Loom (pre-alpha)",
  "oci-model-cache": "Model cache",
  sextant: "Code generation",
  "cloudflare-operator": "Zero Trust ingress",
  trips: "Camera to browser",
  ships: "AIS pipeline",
  stargazer: "Forecast scoring",
  bazel: "One build graph",
};

describe("engineering page", () => {
  it("renders every content entry, snippet, link and diagram before JavaScript", () => {
    const container = new DOMParser().parseFromString(
      prerender("engineering.html"),
      "text/html",
    ).body;
    const page = within(container);
    expect(page.getByRole("heading", { level: 1 }).textContent).toBe(
      intro.title,
    );
    expect(page.getByText(intro.lede)).toBeTruthy();
    expect(
      page.getByRole("link", { name: intro.source.label }).getAttribute("href"),
    ).toBe(intro.source.href);
    expect(
      within(page.getByRole("list", { name: "Platform" }))
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(intro.stickers);
    expect(
      within(page.getByRole("list", { name: "Stack" }))
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(marqueeItems);

    const ids = projects.map((project) => project.id);
    expect(ids).toEqual(Object.keys(CAPTIONS));
    expect(Object.keys(diagrams)).toEqual(ids);
    expect(page.getAllByRole("article")).toHaveLength(ids.length);
    expect(page.getAllByRole("figure")).toHaveLength(ids.length);
    const index = page.getByRole("navigation", { name: "Systems" });
    expect(
      within(index)
        .getAllByRole("link")
        .map((link) => link.getAttribute("href")),
    ).toEqual(ids.map((id) => `#${id}`));

    for (const project of projects) {
      const article = page.getByRole("article", { name: project.title });
      const record = within(article);
      expect(article.id).toBe(project.id);
      expect(record.getByRole("heading", { level: 2 }).textContent).toBe(
        project.title,
      );
      expect(record.getByText(project.oneLiner)).toBeTruthy();
      expect(record.getByText(project.motivation)).toBeTruthy();
      const band = within(
        article.querySelector<HTMLElement>(".engineering-band")!,
      );
      expect(band.getByText(categories[project.category].label)).toBeTruthy();
      if (project.status) expect(band.getByText(project.status)).toBeTruthy();
      expect(
        record.getAllByRole("term").map((term) => term.textContent),
      ).toEqual(project.facts.map((fact) => fact.k));
      expect(
        record.getAllByRole("definition").map((value) => value.textContent),
      ).toEqual(project.facts.map((fact) => fact.v));
      const links = record.queryAllByRole("link");
      expect(links.map((link) => link.getAttribute("href"))).toEqual(
        project.links.map(
          (link) => new URL(link.href, "https://jomcgi.dev").href,
        ),
      );
      for (const [index, link] of links.entries()) {
        expect(link.textContent).toBe(
          project.links[index]!.label +
            (project.links[index]!.href.startsWith("/") ? "" : " ↗"),
        );
      }
      if (project.snippet) {
        expect(article.querySelector("pre")!.dataset.lang).toBe(
          project.snippet.lang,
        );
        expect(article.querySelector("pre code")!.textContent).toBe(
          project.snippet.code,
        );
      } else {
        expect(article.querySelector("pre")).toBeNull();
      }
      const diagram = record.getByRole("figure", {
        name: CAPTIONS[project.id as keyof typeof CAPTIONS],
      });
      expect(diagram.querySelector(".dbox")).toBeTruthy();
      expect(diagram.querySelector("svg path")).toBeTruthy();
    }
    expect(container.querySelector("[style]")).toBeNull();
  });

  it("keeps unmigrated app links on jomcgi.dev when opened from the preview", () => {
    const preview = new DOMParser().parseFromString(
      prerender("engineering.html"),
      "text/html",
    );
    for (const [label, path] of [
      ["Browse the notes", "/app/notes"],
      ["Live trips", "/app/trips"],
    ]) {
      const link = within(preview.body).getByRole("link", { name: label });
      expect(
        new URL(
          link.getAttribute("href")!,
          "https://next.jomcgi.dev/engineering",
        ).href,
      ).toBe(`https://jomcgi.dev${path}`);
    }
  });

  it("hydrates the systems index and disconnects its observer on unmount", () => {
    let onIntersect: IntersectionObserverCallback;
    const observe = vi.fn();
    const disconnect = vi.fn();
    class Observer {
      constructor(callback: IntersectionObserverCallback) {
        onIntersect = callback;
      }
      observe = observe;
      disconnect = disconnect;
    }
    vi.stubGlobal("IntersectionObserver", Observer);
    try {
      const { unmount } = render(
        <Root>
          <EngineeringPage />
        </Root>,
      );
      // StrictMode runs effect setup twice, with cleanup between them.
      expect(
        new Set(observe.mock.calls.map(([element]) => element.id)),
      ).toEqual(new Set(projects.map((project) => project.id)));
      const index = screen.getByRole("navigation", { name: "Systems" });
      expect(index.querySelector("[aria-current]")).toBeNull();
      const entries = ["ships", "trips"].map((id, index) => ({
        target: document.getElementById(id)!,
        isIntersecting: true,
        boundingClientRect: new DOMRect(0, index === 0 ? 200 : 100, 100, 100),
        intersectionRatio: 1,
        intersectionRect: new DOMRect(0, 0, 100, 100),
        rootBounds: null,
        time: 0,
      }));
      act(() => onIntersect(entries, {} as IntersectionObserver));
      expect(
        index.querySelector('[aria-current="location"]')!.getAttribute("href"),
      ).toBe("#trips");
      disconnect.mockClear();
      unmount();
      expect(disconnect).toHaveBeenCalledOnce();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("keeps the hub preview metadata in its HTML entry", () => {
    const html = new DOMParser().parseFromString(
      readFileSync("engineering.html", "utf8"),
      "text/html",
    );
    const title = "Engineering · jomcgi.dev";
    const description =
      "Engineering deep dives: agents, operators, data systems, and build tooling running on a bare-metal Kubernetes homelab.";
    expect(html.title).toBe(title);
    expect(
      html.querySelector('link[rel="canonical"]')!.getAttribute("href"),
    ).toBe("https://jomcgi.dev/engineering");
    for (const [selector, content] of [
      ['name="description"', description],
      ['property="og:type"', "website"],
      ['property="og:site_name"', "Joe McGinley"],
      ['property="og:title"', title],
      ['property="og:description"', description],
      ['property="og:url"', "https://jomcgi.dev/engineering"],
      ['name="twitter:card"', "summary"],
      ['name="twitter:title"', title],
      ['name="twitter:description"', description],
    ]) {
      expect(
        html.querySelector(`meta[${selector}]`)!.getAttribute("content"),
      ).toBe(content);
    }
  });

  it("marks engineering live at its existing URL", () => {
    const section = SECTIONS.find(
      (section) => section.path === "/engineering",
    )!;
    expect(section.live).toBe(true);
    expect(hrefFor(section)).toBe("/engineering");
  });
});
