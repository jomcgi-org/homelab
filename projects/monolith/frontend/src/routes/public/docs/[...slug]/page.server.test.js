import { beforeEach, describe, expect, it, vi } from "vitest";

const { manifest } = vi.hoisted(() => ({ manifest: [] }));

vi.mock("$lib/public/docs/docs-manifest.json", () => ({ default: manifest }));

import {
  cloudflareCacheHeaders,
  DOCS_CACHE_CONTROL,
} from "$lib/cache-headers.js";
import { load } from "./+page.server.js";

function embervmReadme(content) {
  return {
    path: "projects/embervm/README.md",
    slug: "embervm",
    project: "embervm",
    kind: "readme",
    title: "EmberVM",
    order: 0,
    content,
  };
}

function embervmArchitecture(content) {
  return {
    path: "projects/embervm/ARCHITECTURE.md",
    slug: "embervm/architecture",
    project: "embervm",
    kind: "architecture",
    title: "Architecture",
    order: 1,
    content,
  };
}

describe("/docs/[...slug] load", () => {
  beforeEach(() => manifest.splice(0));

  it("returns project, kind, tabs, and html without the leading H1", () => {
    manifest.push(
      embervmReadme(
        "# EmberVM\n\nHello from the readme.\n\n## Section\n\nMore.",
      ),
      embervmArchitecture("# Architecture\n\nBody."),
    );

    const setHeaders = vi.fn();
    const result = load({ params: { slug: "embervm" }, setHeaders });

    expect(result.project).toBe("embervm");
    expect(result.kind).toBe("readme");
    expect(result.tabs.map((tab) => tab.kind)).toEqual([
      "readme",
      "architecture",
    ]);
    expect(result.html).not.toMatch(/<h1\b/);
    expect(result.html).toContain("Hello from the readme.");
    expect(result.html).toContain('<h2 id="section">');
    expect(setHeaders).toHaveBeenCalledWith({
      ...cloudflareCacheHeaders(DOCS_CACHE_CONTROL),
      etag: '"testbuild-docs-embervm"',
    });
  });

  it("throws a 404 for an unknown slug", () => {
    manifest.push(embervmReadme("# EmberVM\n\nHello."));

    expect(() =>
      load({ params: { slug: "missing" }, setHeaders: vi.fn() }),
    ).toThrowError(
      expect.objectContaining({
        status: 404,
        body: { message: "Documentation page not found" },
      }),
    );
  });

  it("does not strip an H1 inside a leading code fence", () => {
    manifest.push(
      embervmReadme("```\n# Not the document title\n```\n\nFence body.\n"),
    );

    const result = load({
      params: { slug: "embervm" },
      setHeaders: vi.fn(),
    });

    expect(result.html).toContain("# Not the document title");
    expect(result.html).toContain("Fence body.");
  });
});
