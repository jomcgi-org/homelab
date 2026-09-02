import { describe, expect, it } from "vitest";

import {
  buildPathIndex,
  buildSidebar,
  getMeta,
  renderDoc,
  resolveDocHref,
  slugifyHeading,
} from "./docs.js";

const MANIFEST = [
  {
    path: "projects/embervm/README.md",
    slug: "embervm",
    project: "embervm",
    kind: "readme",
    title: "EmberVM",
    order: 0,
  },
  {
    path: "projects/embervm/ARCHITECTURE.md",
    slug: "embervm/architecture",
    project: "embervm",
    kind: "architecture",
    title: "EmberVM Architecture",
    order: 1,
  },
  {
    path: "projects/mcp/README.md",
    slug: "mcp",
    project: "mcp",
    kind: "readme",
    title: "Model Context Protocol",
    order: 2,
  },
];

const pathIndex = buildPathIndex(MANIFEST);

describe("slugifyHeading", () => {
  it("lowercases, hyphenates and strips punctuation", () => {
    expect(slugifyHeading("Hello, World!")).toBe("hello-world");
    expect(slugifyHeading("RBAC & Policy")).toBe("rbac-policy");
  });

  it("falls back to 'section' for empty input", () => {
    expect(slugifyHeading("***")).toBe("section");
  });
});

describe("resolveDocHref", () => {
  it("keeps external links and marks them external", () => {
    expect(
      resolveDocHref(
        "https://example.com",
        "projects/embervm/README.md",
        pathIndex,
      ),
    ).toEqual({ href: "https://example.com", external: true });
    expect(
      resolveDocHref("mailto:a@b.c", "projects/embervm/README.md", pathIndex)
        .external,
    ).toBe(true);
  });

  it("keeps in-page anchors and site-absolute links", () => {
    expect(
      resolveDocHref("#intro", "projects/embervm/README.md", pathIndex),
    ).toEqual({ href: "#intro" });
    expect(
      resolveDocHref("/app/trips", "projects/embervm/README.md", pathIndex),
    ).toEqual({ href: "/app/trips" });
  });

  it("resolves a cross-kind link", () => {
    expect(
      resolveDocHref(
        "ARCHITECTURE.md",
        "projects/embervm/README.md",
        pathIndex,
      ),
    ).toEqual({ href: "/docs/embervm/architecture" });
  });

  it("preserves a fragment when rewriting", () => {
    expect(
      resolveDocHref(
        "ARCHITECTURE.md#boot-flow",
        "projects/embervm/README.md",
        pathIndex,
      ),
    ).toEqual({ href: "/docs/embervm/architecture#boot-flow" });
  });

  it("resolves the README through its directory alias", () => {
    expect(
      resolveDocHref(".", "projects/embervm/ARCHITECTURE.md", pathIndex),
    ).toEqual({ href: "/docs/embervm" });
  });

  it("resolves a trailing-slash directory link through the README alias", () => {
    const index = buildPathIndex([
      ...MANIFEST,
      {
        path: "projects/operators/oci-model-cache/README.md",
        slug: "oci-model-cache",
        project: "oci-model-cache",
        kind: "readme",
        title: "OCI Model Cache",
        order: 3,
      },
    ]);
    expect(
      resolveDocHref(
        "../operators/oci-model-cache/",
        "projects/embervm/README.md",
        index,
      ),
    ).toEqual({ href: "/docs/oci-model-cache" });
  });

  it("strips a link to an unpublished doc", () => {
    expect(
      resolveDocHref(
        "runtimes/k3s/README.md",
        "projects/embervm/README.md",
        pathIndex,
      ),
    ).toBeNull();
    expect(
      resolveDocHref(
        "../../docs/decisions/index.md",
        "projects/embervm/README.md",
        pathIndex,
      ),
    ).toBeNull();
  });
});

describe("buildSidebar", () => {
  it("builds projects and existing tabs in manifest order", () => {
    const sidebar = buildSidebar(MANIFEST);

    expect(sidebar.map((project) => project.project)).toEqual([
      "embervm",
      "mcp",
    ]);
    expect(sidebar[0]).toEqual({
      project: "embervm",
      title: "EmberVM",
      slug: "embervm",
      tabs: [
        {
          kind: "readme",
          label: "README",
          slug: "embervm",
          title: "EmberVM",
        },
        {
          kind: "architecture",
          label: "Architecture",
          slug: "embervm/architecture",
          title: "EmberVM Architecture",
        },
      ],
    });
    expect(sidebar[1].tabs.map((tab) => tab.kind)).toEqual(["readme"]);
  });

  it("uses the project name when a README is missing", () => {
    const sidebar = buildSidebar([
      {
        path: "projects/platform/STPA.md",
        slug: "platform/stpa",
        project: "platform",
        kind: "stpa",
        title: "Platform STPA",
        order: 0,
      },
    ]);

    expect(sidebar[0]).toMatchObject({
      project: "platform",
      title: "platform",
      slug: "platform/stpa",
    });
    expect(sidebar[0].tabs.map((tab) => tab.kind)).toEqual(["stpa"]);
  });

  it("uses fixed document-kind order even if entry order differs", () => {
    const sidebar = buildSidebar([
      { ...MANIFEST[1], order: 0 },
      { ...MANIFEST[0], order: 1 },
    ]);

    expect(sidebar[0].tabs.map((tab) => tab.kind)).toEqual([
      "readme",
      "architecture",
    ]);
  });
});

describe("renderDoc", () => {
  const content = [
    "# Doc Title",
    "",
    "Intro paragraph for the meta.",
    "",
    "## Section One",
    "",
    "See [architecture](ARCHITECTURE.md), [internal notes](NOTES.md) and [home](https://example.com).",
    "",
    "### Sub Heading",
    "",
    "```mermaid",
    "graph LR",
    "A-->B",
    "```",
    "",
    "<div>raw html</div>",
    "",
  ].join("\n");

  const { html, toc } = renderDoc(
    { path: "projects/embervm/README.md", content },
    pathIndex,
  );

  it("rewrites intra-doc links to public document slugs", () => {
    expect(html).toContain('href="/docs/embervm/architecture"');
  });

  it("strips links to unpublished docs but keeps the text", () => {
    expect(html).toContain("internal notes");
    expect(html).not.toContain("NOTES.md");
  });

  it("opens external links in a new tab", () => {
    expect(html).toContain('href="https://example.com"');
    expect(html).toContain('target="_blank"');
  });

  it("assigns heading ids and builds a depth 2-3 TOC", () => {
    expect(html).toContain('<h2 id="section-one">');
    expect(html).toContain('<h3 id="sub-heading">');
    expect(toc).toEqual([
      { depth: 2, text: "Section One", id: "section-one" },
      { depth: 3, text: "Sub Heading", id: "sub-heading" },
    ]);
  });

  it("renders a mermaid fence as a labelled source block", () => {
    expect(html).toContain('data-lang="mermaid"');
    expect(html).toContain('class="doc-code doc-mermaid"');
    expect(html).toContain("A--&gt;B");
  });

  it("escapes raw HTML rather than passing it through", () => {
    expect(html).toContain("&lt;div&gt;raw html&lt;/div&gt;");
    expect(html).not.toContain("<div>raw html</div>");
  });

  it("renders a validated keyed figure at block level", () => {
    const result = renderDoc(
      {
        path: "docs/posts/2026-01-15-example.md",
        content: "![Memory tiers](figures/memory.svg)\n",
        figures: {
          "figures/memory.svg":
            '<svg viewBox="0 0 10 10"><path d="M0 0L10 10"/></svg>',
        },
      },
      new Map(),
    );

    expect(result.html).toContain('<figure class="fig">');
    expect(result.html).toContain('<div class="fig-art"><svg');
    expect(result.html).toContain("<figcaption>Memory tiers</figcaption>");
    expect(result.html).not.toContain("<p><figure");
  });

  it("keeps image rendering unchanged when an entry has no figures", () => {
    const result = renderDoc(
      {
        path: "docs/posts/2026-01-15-example.md",
        content: "![Photo](https://example.com/photo.png)\n",
      },
      new Map(),
    );

    expect(result.html).toContain(
      '<p><img src="https://example.com/photo.png" alt="Photo" loading="lazy" /></p>',
    );
    expect(result.html).not.toContain('<figure class="fig">');
  });

  it("de-duplicates repeated heading ids", () => {
    const duplicate = ["## Notes", "", "x", "", "## Notes", "", "y"].join("\n");
    const result = renderDoc(
      { path: "projects/embervm/README.md", content: duplicate },
      pathIndex,
    );
    expect(result.toc.map((heading) => heading.id)).toEqual([
      "notes",
      "notes-1",
    ]);
  });
});

describe("getMeta", () => {
  it("derives a description from the first prose paragraph", () => {
    const meta = getMeta({
      title: "Security Model",
      content: "# Security Model\n\nThis is the first paragraph.\n\nSecond.",
    });
    expect(meta.title).toBe("Security Model");
    expect(meta.description).toBe("This is the first paragraph.");
  });
});

describe("figure key tables", () => {
  const svg =
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">' +
    '<circle cx="1" cy="1" r="1" data-key="A" data-tone="gpu"/></svg>';
  const table = "| Key | Part |\n|---|---|\n| A | GPU |\n| 2 | PCIe |\n";

  it("draws the key column as callouts in the figure's tones", () => {
    const { html } = renderDoc(
      {
        path: "docs/posts/x.md",
        content: `![Tiers](figures/t.svg)\n\n${table}`,
        figures: { "figures/t.svg": svg },
      },
      buildPathIndex([]),
    );

    expect(html).toContain('<table class="fig-key">');
    expect(html).toContain(
      '<td class="key" data-tone="gpu"><span class="co">A</span></td>',
    );
    expect(html).toContain('<td class="key"><span class="co">2</span></td>');
    expect(html).toContain("<td>GPU</td>");
  });

  it("leaves a table that does not follow a figure alone", () => {
    const { html } = renderDoc(
      {
        path: "docs/posts/x.md",
        content: `Intro.\n\n${table}`,
        figures: { "figures/t.svg": svg },
      },
      buildPathIndex([]),
    );

    expect(html).not.toContain("fig-key");
    expect(html).toContain("<td>A</td>");
  });
});
