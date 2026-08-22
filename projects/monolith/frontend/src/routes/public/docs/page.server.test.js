import { beforeEach, describe, expect, it, vi } from "vitest";

const { manifest } = vi.hoisted(() => ({ manifest: [] }));

vi.mock("$lib/public/docs/docs-manifest.json", () => ({ default: manifest }));

import { DOCS_CACHE_CONTROL } from "$lib/cache-headers.js";
import { load } from "./+page.server.js";
import { buildProjectCards } from "$lib/server/docs.js";

describe("/docs load", () => {
  beforeEach(() => manifest.splice(0));

  it("returns project cards with truncated excerpts and fixed tab states", () => {
    manifest.push(
      {
        path: "projects/embervm/README.md",
        slug: "embervm",
        project: "embervm",
        kind: "readme",
        title: "EmberVM",
        order: 0,
        content: `# EmberVM\n\n**${"a".repeat(220)}**\n\nSecond paragraph.`,
      },
      {
        path: "projects/embervm/ARCHITECTURE.md",
        slug: "embervm/architecture",
        project: "embervm",
        kind: "architecture",
        title: "Architecture",
        order: 1,
        content: "# Architecture",
      },
    );

    const cards = buildProjectCards(manifest);

    expect(cards).toHaveLength(1);
    expect(cards[0].excerpt).toHaveLength(200);
    expect(
      cards[0].tabs.map((tab) => [tab.kind, tab.slug, tab.disabled]),
    ).toEqual([
      ["readme", "embervm", false],
      ["architecture", "embervm/architecture", false],
      ["stpa", null, true],
      ["threat-model", null, true],
    ]);
  });

  it("sets cache metadata and returns no document bodies", () => {
    manifest.push({
      path: "projects/mcp/README.md",
      slug: "mcp",
      project: "mcp",
      kind: "readme",
      title: "MCP",
      order: 0,
      content: "# MCP\n\nModel context protocol.",
    });
    const setHeaders = vi.fn();

    const result = load({ setHeaders });

    expect(result.meta.description).toBe(
      "Current-state documentation for the public projects, rendered from the repository.",
    );
    expect(result.projects[0]).not.toHaveProperty("content");
    expect(setHeaders).toHaveBeenCalledWith({
      "cache-control": DOCS_CACHE_CONTROL,
    });
  });
});
