// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { mount, tick, unmount } from "svelte";
import Page from "./+page.svelte";
import { MCP_URL, METHOD_URL, SPEC_URL, listings } from "./scotland-data.js";

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
});

async function render() {
  instance = mount(Page, { target });
  await tick();
  return target;
}

describe("scotland landing page", () => {
  it("is one technical-drawing sheet that names the MCP URL", async () => {
    const root = await render();
    expect(root.querySelector("main.td.sheet")).not.toBeNull();
    expect(MCP_URL).toBe("https://jomcgi.dev/scotland/mcp");
    expect(root.querySelector(".spec dd.mono").textContent).toBe(MCP_URL);
    expect(root.querySelector('pre[data-lang="sh"]').textContent).toContain(
      MCP_URL,
    );
  });

  it("links the API contract and the method", async () => {
    const root = await render();
    const hrefs = [...root.querySelectorAll("a")].map((a) =>
      a.getAttribute("href"),
    );
    expect(hrefs).toContain(SPEC_URL);
    expect(hrefs).toContain(METHOD_URL);
    expect(SPEC_URL).toBe("https://jomcgi.dev/scotland/v1/openapi.json");
  });

  it("indexes every section by an anchor that exists", async () => {
    const root = await render();
    const anchors = [...root.querySelectorAll(".spine .index-list a")]
      .map((a) => a.getAttribute("href"))
      .filter((h) => h.startsWith("#"));
    expect(anchors.length).toBeGreaterThan(0);
    for (const href of anchors) {
      expect(root.querySelector(href)).not.toBeNull();
    }
  });

  it("lists unreleased directory listings without a dead link", async () => {
    const root = await render();
    const items = [...root.querySelectorAll(".listings li")];
    expect(items).toHaveLength(listings.length);
    listings.forEach((l, i) => {
      const link = items[i].querySelector("a");
      if (l.href) expect(link.getAttribute("href")).toBe(l.href);
      else expect(link).toBeNull();
    });
  });
});
