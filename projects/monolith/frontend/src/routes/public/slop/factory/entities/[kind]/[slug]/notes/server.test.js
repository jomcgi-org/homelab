import { describe, expect, it, vi } from "vitest";
import { GET } from "./+server.js";

const headers = { get: () => null };

describe("/public/slop/factory/entities/[kind]/[slug]/notes GET", () => {
  it("encodes the entity path and forwards filters", async () => {
    const payload = { entity: { slug: "mcp gateway" }, notes: [] };
    const fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers,
      json: async () => payload,
    });
    const url = new URL(
      "https://jomcgi.dev/slop/factory/entities/project/mcp/notes?state=verified&limit=10",
    );

    const response = await GET({
      fetch,
      params: { kind: "project", slug: "mcp gateway" },
      setHeaders: vi.fn(),
      url,
    });

    const upstream = fetch.mock.calls[0][0];
    expect(upstream).toContain(
      "/api/knowledge/public/entities/project/mcp%20gateway/notes",
    );
    expect(upstream).toContain("state=verified&limit=10");
    expect(await response.json()).toEqual(payload);
  });

  it("keeps missing entities as 404", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: false, status: 404 });
    const url = new URL(
      "https://jomcgi.dev/slop/factory/entities/project/x/notes",
    );

    await expect(
      GET({
        fetch,
        params: { kind: "project", slug: "x" },
        setHeaders: vi.fn(),
        url,
      }),
    ).rejects.toMatchObject({ status: 404 });
  });
});
