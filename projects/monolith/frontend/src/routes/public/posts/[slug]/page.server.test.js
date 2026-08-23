import { describe, expect, it, vi } from "vitest";

vi.mock("$lib/public/posts/posts-manifest.json", () => ({
  default: [
    {
      path: "docs/posts/2026-01-15-example.md",
      slug: "example",
      title: "Example",
      date: "2026-01-15",
      summary: "One sentence.",
      content:
        "# Body\n\nHello **world**.\n\n[rel](./other.md)\n\n" +
        '[link](https://example.com)\n\n<script>alert("nope")</script>\n',
    },
  ],
}));

import {
  cloudflareCacheHeaders,
  DOCS_CACHE_CONTROL,
} from "$lib/cache-headers.js";
import { load } from "./+page.server.js";

describe("/public/posts/[slug] load", () => {
  it("throws a 404 for an unknown slug", () => {
    expect(() =>
      load({ params: { slug: "missing" }, setHeaders: vi.fn() }),
    ).toThrowError(
      expect.objectContaining({
        status: 404,
        body: { message: "Post not found" },
      }),
    );
  });

  it("renders the post body as HTML", () => {
    const setHeaders = vi.fn();

    const result = load({ params: { slug: "example" }, setHeaders });

    expect(result.html).toContain('<h1 id="body">Body</h1>');
    expect(result.html).toContain("Hello <strong>world</strong>.");
    expect(result.summary).toBe("One sentence.");
    expect(setHeaders).toHaveBeenCalledWith({
      ...cloudflareCacheHeaders(DOCS_CACHE_CONTROL),
      etag: '"testbuild-posts-example"',
    });
  });

  it("renders an unpublished relative markdown link as plain text", () => {
    const result = load({
      params: { slug: "example" },
      setHeaders: vi.fn(),
    });

    expect(result.html).toContain("<p>rel</p>");
    expect(result.html).not.toContain('href="./other.md"');
  });

  it("keeps external link safety attributes", () => {
    const result = load({
      params: { slug: "example" },
      setHeaders: vi.fn(),
    });

    expect(result.html).toContain(
      '<a href="https://example.com" target="_blank" rel="noopener noreferrer">link</a>',
    );
  });

  it("escapes raw script tags", () => {
    const result = load({
      params: { slug: "example" },
      setHeaders: vi.fn(),
    });

    expect(result.html).toContain('&lt;script&gt;alert("nope")&lt;/script&gt;');
    expect(result.html).not.toContain("<script>");
  });
});
