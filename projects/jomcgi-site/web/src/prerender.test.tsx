import { readFileSync } from "node:fs";
import { act } from "react";
import { hydrateRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";
import { PAGES } from "./pages";
import { MARK, render } from "./prerender";
import { Root } from "./root";
import { SECTIONS, hrefFor } from "./sections";

describe.each(Object.entries(PAGES))("%s", (entry, Page) => {
  it("has one marker for build.mjs to fill", () => {
    expect(readFileSync(entry, "utf8").split(MARK)).toHaveLength(2);
  });

  it("renders its heading and every nav link before any script runs", () => {
    const container = document.createElement("div");
    container.innerHTML = render(entry);
    expect(container.querySelectorAll("h1")).toHaveLength(1);
    const nav = container.querySelector('nav[aria-label="Site"]')!;
    expect(
      [...nav.querySelectorAll("a")].map((a) => a.getAttribute("href")),
    ).toEqual(
      SECTIONS.filter((section) => section.listed !== false).map(hrefFor),
    );
  });

  it("hydrates the rendered HTML without a mismatch", async () => {
    const container = document.createElement("div");
    container.innerHTML = render(entry);
    document.body.append(container);
    const errors: unknown[] = [];
    await act(async () => {
      hydrateRoot(
        container,
        <Root>
          <Page />
        </Root>,
        { onRecoverableError: (error) => errors.push(error) },
      );
    });
    expect(errors).toEqual([]);
    container.remove();
  });
});

describe("prerendered", () => {
  it("accepts a filled root and rejects an empty one or a leftover marker", async () => {
    const { prerendered } = await import("../prerendered.mjs");
    expect(prerendered('<div id="root"><div class="site"></div></div>')).toBe(
      true,
    );
    expect(prerendered('<div id="root"></div>')).toBe(false);
    expect(prerendered('<div id="root"><!--ssr--></div>')).toBe(false);
  });
});
