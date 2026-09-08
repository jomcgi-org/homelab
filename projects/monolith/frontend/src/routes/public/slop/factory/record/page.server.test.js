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

const facts = {
  daily: [],
  totals: { verified: 4, unverified: 2, disputed: 1 },
  contradictions: 3,
};

function response(path, failedPath = "") {
  if (path === failedPath) return Promise.resolve({ ok: false, status: 503 });
  if (path === "/slop/factory/entities") return Promise.resolve(ok(entities));
  if (path === "/slop/factory/facts") return Promise.resolve(ok(facts));
  if (path.startsWith("/slop/factory/search")) {
    return Promise.resolve(ok([{ note_id: "fact" }]));
  }
  return Promise.resolve(
    ok({ entity: { slug: "embervm" }, notes: [], contradictions: [] }),
  );
}

describe("factory record loader", () => {
  it("loads and sorts the project spine", async () => {
    const fetch = vi.fn(response);
    const result = await load({
      fetch,
      setHeaders: vi.fn(),
      url: new URL("https://jomcgi.dev/slop/factory/record"),
    });

    expect(result.projects.map((item) => item.slug)).toEqual(["embervm"]);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(result.facts).toEqual(facts);
  });

  it("loads a selected entity chapter through its proxy", async () => {
    const chapter = {
      entity: { slug: "embervm" },
      notes: [],
      contradictions: [],
    };
    const fetch = vi.fn((path) =>
      Promise.resolve(
        ok(
          path === "/slop/factory/entities"
            ? entities
            : path === "/slop/factory/facts"
              ? facts
              : chapter,
        ),
      ),
    );
    const result = await load({
      fetch,
      setHeaders: vi.fn(),
      url: new URL("https://jomcgi.dev/slop/factory/record?entity=embervm"),
    });

    expect(fetch.mock.calls.map(([path]) => path)).toContain(
      "/slop/factory/entities/project/embervm/notes?state=verified%2Cunverified&limit=60",
    );
    expect(result.chapter).toEqual(chapter);
  });

  it("loads search with the selected mode and caps the query", async () => {
    const fetch = vi.fn(response);
    const result = await load({
      fetch,
      setHeaders: vi.fn(),
      url: new URL(
        `https://jomcgi.dev/slop/factory/record?q=${"x".repeat(220)}&mode=semantic`,
      ),
    });

    expect(result.q).toHaveLength(200);
    expect(result.mode).toBe("semantic");
    expect(fetch.mock.calls.map(([path]) => path).join(" ")).toContain(
      "mode=semantic",
    );
  });

  it.each([
    ["/slop/factory/entities", "entities", ""],
    ["/slop/factory/facts", "facts", ""],
    [
      "/slop/factory/entities/project/embervm/notes?state=verified%2Cunverified&limit=60",
      "chapter",
      "?entity=embervm",
    ],
    ["search", "search", "?q=ember"],
  ])(
    "keeps rendering when %s returns 503",
    async (failedPath, section, query) => {
      const fetch = vi.fn((path) =>
        response(
          path,
          failedPath === "search" && path.startsWith("/slop/factory/search")
            ? path
            : failedPath,
        ),
      );

      const result = await load({
        fetch,
        setHeaders: vi.fn(),
        url: new URL(`https://jomcgi.dev/slop/factory/record${query}`),
      });

      expect(result.unavailable[section]).toBe(true);
      expect(result.title).toBe("Factory record");
    },
  );
});
