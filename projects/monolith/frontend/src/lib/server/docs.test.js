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
    path: "projects/firecracker/README.md",
    slug: "projects/firecracker",
    title: "Firecracker",
    section: "Projects",
    order: 0,
  },
  {
    path: "projects/monolith/knowledge/README.md",
    slug: "projects/monolith/knowledge",
    title: "Knowledge",
    section: "Projects",
    order: 1,
  },
  {
    path: "projects/monolith/README.md",
    slug: "projects/monolith",
    title: "Monolith",
    section: "Projects",
    order: 2,
  },
  {
    path: "docs/decisions/index.md",
    slug: "decisions",
    title: "ADRs",
    section: "Decisions",
    order: 3,
  },
  {
    path: "docs/decisions/agents/001-a.md",
    slug: "decisions/agents/001-a",
    title: "Agent One",
    section: "Decisions",
    order: 4,
  },
  {
    path: "docs/decisions/agents/002-b.md",
    slug: "decisions/agents/002-b",
    title: "Agent Two",
    section: "Decisions",
    order: 5,
  },
  {
    path: "docs/decisions/security/001-s.md",
    slug: "decisions/security/001-s",
    title: "Sec One",
    section: "Decisions",
    order: 6,
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
      resolveDocHref("https://example.com", "docs/security.md", pathIndex),
    ).toEqual({
      href: "https://example.com",
      external: true,
    });
    expect(
      resolveDocHref("mailto:a@b.c", "docs/security.md", pathIndex).external,
    ).toBe(true);
  });

  it("keeps in-page anchors and site-absolute links", () => {
    expect(resolveDocHref("#intro", "docs/security.md", pathIndex)).toEqual({
      href: "#intro",
    });
    expect(resolveDocHref("/app/trips", "docs/security.md", pathIndex)).toEqual(
      { href: "/app/trips" },
    );
  });

  it("rewrites a relative link to a published doc into a /docs slug", () => {
    expect(
      resolveDocHref(
        "knowledge/README.md",
        "projects/monolith/README.md",
        pathIndex,
      ),
    ).toEqual({
      href: "/docs/projects/monolith/knowledge",
    });
    // from a sibling ADR up to another category
    expect(
      resolveDocHref(
        "../security/001-s.md",
        "docs/decisions/agents/001-a.md",
        pathIndex,
      ),
    ).toEqual({ href: "/docs/decisions/security/001-s" });
  });

  it("preserves a fragment when rewriting", () => {
    expect(
      resolveDocHref(
        "knowledge/README.md#deploy",
        "projects/monolith/README.md",
        pathIndex,
      ),
    ).toEqual({
      href: "/docs/projects/monolith/knowledge#deploy",
    });
  });

  it("resolves an index doc via its directory alias", () => {
    expect(
      resolveDocHref(
        "../index.md",
        "docs/decisions/agents/001-a.md",
        pathIndex,
      ),
    ).toEqual({
      href: "/docs/decisions",
    });
  });

  it("resolves a README doc via its directory alias", () => {
    expect(
      resolveDocHref("..", "projects/monolith/knowledge/README.md", pathIndex),
    ).toEqual({
      href: "/docs/projects/monolith",
    });
  });

  it("strips a link to an unpublished doc (returns null)", () => {
    expect(
      resolveDocHref("plans/secret.md", "docs/security.md", pathIndex),
    ).toBeNull();
    expect(
      resolveDocHref("../../.claude/AGENTS.md", "docs/security.md", pathIndex),
    ).toBeNull();
  });
});

describe("buildSidebar", () => {
  it("nests project READMEs by path and groups ADRs by category", () => {
    const s = buildSidebar(MANIFEST);
    expect(s.projects.map((p) => p.name)).toEqual(["firecracker", "monolith"]);

    const firecracker = s.projects[0];
    expect(firecracker.slug).toBe("projects/firecracker");
    expect(firecracker.title).toBe("Firecracker");
    expect(firecracker.children).toEqual([]);

    const monolith = s.projects[1];
    expect(monolith.slug).toBe("projects/monolith");
    expect(monolith.children.map((c) => c.slug)).toEqual([
      "projects/monolith/knowledge",
    ]);
    expect(monolith.children[0].children).toEqual([]);

    expect(s.decisions.index.slug).toBe("decisions");
    expect(s.decisions.categories.map((c) => c.name)).toEqual([
      "agents",
      "security",
    ]);
    expect(s.decisions.categories[0].items.map((i) => i.slug)).toEqual([
      "decisions/agents/001-a",
      "decisions/agents/002-b",
    ]);
  });

  it("gives a group node a null slug when its directory has no README", () => {
    const manifest = [
      {
        path: "projects/platform/linkerd/README.md",
        slug: "projects/platform/linkerd",
        title: "Linkerd",
        section: "Projects",
        order: 0,
      },
    ];
    const s = buildSidebar(manifest);
    expect(s.projects[0]).toMatchObject({ name: "platform", slug: null });
    expect(s.projects[0].children[0]).toMatchObject({
      name: "linkerd",
      slug: "projects/platform/linkerd",
    });
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
    "See [knowledge](knowledge/README.md), [internal plan](plans/secret.md) and [home](https://example.com).",
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
    { path: "projects/monolith/README.md", content },
    pathIndex,
  );

  it("rewrites intra-doc links to /docs slugs", () => {
    expect(html).toContain('href="/docs/projects/monolith/knowledge"');
  });

  it("strips links to unpublished docs but keeps the text", () => {
    expect(html).toContain("internal plan");
    expect(html).not.toContain("plans/secret");
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

  it("renders a mermaid fence as a labelled source block marked for client rendering", () => {
    expect(html).toContain('data-lang="mermaid"');
    expect(html).toContain('class="doc-code doc-mermaid"');
    expect(html).toContain("A--&gt;B");
  });

  it("escapes raw HTML rather than passing it through", () => {
    expect(html).toContain("&lt;div&gt;raw html&lt;/div&gt;");
    expect(html).not.toContain("<div>raw html</div>");
  });

  it("de-duplicates repeated heading ids", () => {
    const dup = ["## Notes", "", "x", "", "## Notes", "", "y"].join("\n");
    const r = renderDoc({ path: "docs/security.md", content: dup }, pathIndex);
    expect(r.toc.map((t) => t.id)).toEqual(["notes", "notes-1"]);
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
