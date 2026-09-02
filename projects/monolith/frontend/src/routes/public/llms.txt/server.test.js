import { beforeEach, describe, expect, it, vi } from "vitest";

const { manifest } = vi.hoisted(() => ({ manifest: [] }));

vi.mock("$lib/public/posts/posts-manifest.json", () => ({
  default: manifest,
}));

import { GET } from "./+server.js";

describe("/public/llms.txt", () => {
  beforeEach(() => {
    manifest.splice(0);
  });

  it("has no blog section until a post is published", async () => {
    const body = await GET().text();

    expect(body).not.toContain("## Blog");
    expect(body).toContain("\n\n## Elsewhere");
  });

  it("lists posts newest first with a blank line before Elsewhere", async () => {
    manifest.push(
      { slug: "older", title: "Older", date: "2026-08-01", summary: "A." },
      { slug: "newer", title: "Newer", date: "2026-09-01", summary: "B." },
    );

    const body = await GET().text();

    expect(body).toContain(
      "## Blog\n\n- [Newer](https://jomcgi.dev/blog/newer): B.\n- [Older](https://jomcgi.dev/blog/older): A.\n\n## Elsewhere",
    );
  });
});
