// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createRawSnippet, mount, tick, unmount } from "svelte";
import DocsShell from "./DocsShell.svelte";
import DocsTabs from "./DocsTabs.svelte";

const sidebar = [
  {
    project: "embervm",
    title: "EmberVM",
    slug: "embervm",
    tabs: [
      { kind: "readme", label: "README", slug: "embervm", title: "EmberVM" },
      {
        kind: "architecture",
        label: "Architecture",
        slug: "embervm/architecture",
        title: "EmberVM Architecture",
      },
    ],
  },
  {
    project: "sextant",
    title: "sextant",
    slug: "sextant",
    tabs: [
      { kind: "readme", label: "README", slug: "sextant", title: "sextant" },
    ],
  },
];

const children = createRawSnippet(() => ({
  render: () => "<article class='edition'>doc</article>",
}));

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

async function render(component, props) {
  instance = mount(component, { target, props });
  await tick();
  return target;
}

describe("DocsShell", () => {
  it("renders the sheet with search, projects and site sections in the spine", async () => {
    const root = await render(DocsShell, { sidebar, activeSlug: "", children });
    expect(root.querySelector("main.td.sheet")).not.toBeNull();
    expect(root.querySelector('.spine input[type="search"]')).not.toBeNull();
    const groups = [
      ...root.querySelectorAll('.spine nav[aria-label="Projects"] details'),
    ];
    expect(
      groups.map((g) => g.querySelector(".group-name").textContent),
    ).toEqual(["EmberVM", "sextant"]);
    expect(groups.every((g) => !g.open)).toBe(true);
    expect(
      root
        .querySelector('nav[aria-label="Site"] a[href="/docs"]')
        .getAttribute("aria-current"),
    ).toBe("page");
    expect(root.querySelector('nav[aria-label="On this page"]')).toBeNull();
    expect(root.querySelector(".content .edition").textContent).toBe("doc");
  });

  it("opens the active project, marks its document, and indexes the page", async () => {
    const root = await render(DocsShell, {
      sidebar,
      activeSlug: "embervm/architecture",
      current: "EmberVM Architecture",
      toc: [
        { depth: 2, text: "1. What EmberVM is", id: "what" },
        { depth: 3, text: "Capability matrix", id: "matrix" },
        { depth: 2, text: "2. System overview", id: "overview" },
      ],
      children,
    });
    const groups = [
      ...root.querySelectorAll('.spine nav[aria-label="Projects"] details'),
    ];
    expect(groups.map((g) => g.open)).toEqual([true, false]);
    const active = root.querySelector(
      '.spine nav[aria-label="Projects"] a.active',
    );
    expect(active.getAttribute("href")).toBe("/docs/embervm/architecture");
    expect(active.getAttribute("aria-current")).toBe("page");

    const entries = [
      ...root.querySelectorAll('nav[aria-label="On this page"] a'),
    ];
    expect(entries.map((a) => a.getAttribute("href"))).toEqual([
      "#what",
      "#matrix",
      "#overview",
    ]);
    expect(
      entries.map((a) => a.querySelector(".num")?.textContent ?? ""),
    ).toEqual(["1.", "", "2."]);
    expect(entries[1].classList.contains("sub")).toBe(true);
    expect(root.querySelector(".trail .title").textContent).toBe(
      "EmberVM Architecture",
    );
  });
});

describe("DocsTabs", () => {
  it("draws only published documents, underlining the current one", async () => {
    const root = await render(DocsTabs, {
      tabs: [
        { kind: "readme", label: "README", slug: "mcp", title: "MCP" },
        {
          kind: "architecture",
          label: "Architecture",
          slug: "mcp/architecture",
          title: "MCP Architecture",
        },
        {
          kind: "stpa",
          label: "STPA",
          slug: null,
          title: "STPA",
          disabled: true,
        },
      ],
      activeKind: "architecture",
    });
    const tabs = [...root.querySelectorAll(".tabs a")];
    expect(tabs.map((a) => a.textContent)).toEqual(["README", "Architecture"]);
    expect(tabs[1].classList.contains("here")).toBe(true);
    expect(tabs[1].getAttribute("aria-current")).toBe("page");
    expect(root.querySelector(".tabs span")).toBeNull();
  });
});
