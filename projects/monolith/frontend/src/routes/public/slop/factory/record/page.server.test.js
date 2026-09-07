import { describe, expect, it, vi } from "vitest";
import { load } from "./+page.server.js";

const entities = [
  {
    kind: "project",
    slug: "embervm",
    title: "EmberVM",
    note_counts: { verified: 4, unverified: 2 },
  },
  {
    kind: "service",
    slug: "postgres",
    title: "Postgres",
    note_counts: { verified: 20, unverified: 0 },
  },
];

function ok(data) {
  return { ok: true, headers: { get: () => null }, json: async () => data };
}

describe("factory record loader", () => {
  it("loads and sorts the project spine", async () => {
    const fetch = vi.fn().mockResolvedValue(ok(entities));
    const result = await load({
      fetch,
      setHeaders: vi.fn(),
      url: new URL("https://jomcgi.dev/slop/factory/record"),
    });

    expect(result.projects.map((item) => item.slug)).toEqual(["embervm"]);
    expect(fetch).toHaveBeenCalledOnce();
  });

  it("loads a selected entity chapter through its proxy", async () => {
    const chapter = {
      entity: { slug: "embervm" },
      notes: [],
      contradictions: [],
    };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(ok(entities))
      .mockResolvedValueOnce(ok(chapter));
    const result = await load({
      fetch,
      setHeaders: vi.fn(),
      url: new URL("https://jomcgi.dev/slop/factory/record?entity=embervm"),
    });

    expect(fetch.mock.calls[1][0]).toContain(
      "/slop/factory/entities/project/embervm/notes",
    );
    expect(result.chapter).toEqual(chapter);
  });

  it("loads search with the selected mode and caps the query", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(ok(entities))
      .mockResolvedValueOnce(ok([{ note_id: "fact" }]));
    const result = await load({
      fetch,
      setHeaders: vi.fn(),
      url: new URL(
        `https://jomcgi.dev/slop/factory/record?q=${"x".repeat(220)}&mode=semantic`,
      ),
    });

    expect(result.q).toHaveLength(200);
    expect(result.mode).toBe("semantic");
    expect(fetch.mock.calls[1][0]).toContain("mode=semantic");
  });

  it("returns 404 for an unknown well-formed entity", async () => {
    const fetch = vi.fn().mockResolvedValue(ok(entities));

    await expect(
      load({
        fetch,
        setHeaders: vi.fn(),
        url: new URL("https://jomcgi.dev/slop/factory/record?entity=missing"),
      }),
    ).rejects.toMatchObject({ status: 404 });
  });
});
