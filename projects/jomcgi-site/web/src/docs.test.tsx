import { readFileSync } from "node:fs";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { describe, expect, it } from "vitest";
import manifest from "../docs-manifest.json";
import { DOC_PAGES } from "../docs/_src/pages";
import sidebar from "../docs/_src/sidebar.json";
import { DocsIndexPage } from "./docs-index-page";
import { Root } from "./root";
import { render as prerender } from "./prerender";
import { search } from "./docs-search-index";
import { SECTIONS, hrefFor } from "./sections";

describe("static docs", () => {
  it("links Docs to this site's live section", () => {
    const docs = SECTIONS.find((section) => section.path === "/docs")!;
    expect(docs.live).toBe(true);
    expect(hrefFor(docs)).toBe("/docs");
  });

  it("registers exactly one page per manifest entry", () => {
    expect(Object.keys(DOC_PAGES)).toEqual(
      manifest.map((entry) => `docs/${entry.slug}.html`),
    );
  });

  it("links every public project and tab from the overview", () => {
    render(
      <Root>
        <DocsIndexPage />
      </Root>,
    );
    const projects = screen.getByRole("region", { name: "Public projects" });
    for (const project of sidebar) {
      const tabs = within(projects).getByRole("navigation", {
        name: `${project.project} documents`,
      });
      expect(
        within(tabs)
          .getAllByRole("link")
          .map((a) => a.getAttribute("href")),
      ).toEqual(project.tabs.map((tab) => `/docs/${tab.slug}`));
    }
  });

  it.each(manifest)(
    "$slug preserves metadata, links and heading anchors",
    (entry) => {
      const doc = new DOMParser().parseFromString(
        readFileSync(`docs/${entry.slug}.html`, "utf8"),
        "text/html",
      );
      expect(doc.title).toBe(`${entry.title} · Docs · jomcgi.dev`);
      const description = doc
        .querySelector('meta[name="description"]')!
        .getAttribute("content");
      expect(description).toBeTruthy();
      expect(
        doc.querySelector('link[rel="canonical"]')!.getAttribute("href"),
      ).toBe(`https://jomcgi.dev/docs/${entry.slug}`);
      for (const [attr, key, value] of [
        ["property", "og:type", "article"],
        ["property", "og:site_name", "Joe McGinley"],
        ["property", "og:title", doc.title],
        ["property", "og:description", description],
        ["property", "og:url", `https://jomcgi.dev/docs/${entry.slug}`],
        ["name", "twitter:card", "summary"],
        ["name", "twitter:title", doc.title],
        ["name", "twitter:description", description],
      ])
        expect(
          doc.querySelector(`meta[${attr}="${key}"]`)!.getAttribute("content"),
        ).toBe(value);
      const page = new DOMParser().parseFromString(
        prerender(`docs/${entry.slug}.html`),
        "text/html",
      );
      for (const a of page.querySelectorAll(
        'nav[aria-label="On this page"] a',
      )) {
        expect(
          page.getElementById(a.getAttribute("href")!.slice(1)),
        ).toBeTruthy();
      }
      expect(page.querySelectorAll("[style], script, style")).toHaveLength(0);
      const current = page.querySelector(
        'nav[aria-label="Project documents"] [aria-current="page"]',
      );
      expect(current?.getAttribute("href")).toBe(`/docs/${entry.slug}`);
    },
  );

  it("searches document bodies and only returns public routes", () => {
    expect(search("microvm").length).toBeGreaterThan(0);
    const slugs = new Set(manifest.map((entry) => entry.slug));
    for (const result of search("architecture"))
      expect(slugs.has(result.slug)).toBe(true);
    expect(search(" ")).toEqual([]);
    expect(search("no-such-public-document-xyz")).toEqual([]);
  });

  it("supports the search shortcut, result links and Escape", async () => {
    render(
      <Root>
        <DocsIndexPage />
      </Root>,
    );
    const input = screen.getByRole("searchbox", {
      name: "Search documentation",
    });
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(document.activeElement).toBe(input);
    fireEvent.change(input, { target: { value: "microvm" } });
    await waitFor(() =>
      expect(screen.getByRole("list", { name: "Search results" })).toBeTruthy(),
    );
    const results = screen.getByRole("list", { name: "Search results" });
    const first = within(results).getAllByRole("link")[0]!;
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(document.activeElement).toBe(first);
    expect(first.getAttribute("href")).toMatch(/^\/docs\//);
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByRole("list", { name: "Search results" })).toBeNull();
  });
});
