import { describe, expect, it, vi } from "vitest";
import { load } from "./+page.server.js";

const entities = [
  {
    kind: "project",
    slug: "embervm",
    title: "EmberVM",
    note_counts: { verified: 10, unverified: 6 },
  },
  {
    kind: "project",
    slug: "threshold",
    title: "Threshold",
    note_counts: { verified: 10, unverified: 5 },
  },
  {
    kind: "project",
    slug: "small-project",
    title: "Small project",
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

describe("factory context loader", () => {
  it("includes 16-atom projects and excludes 15-atom projects", async () => {
    const fetch = vi.fn(response);
    const result = await load({
      fetch,
      setHeaders: vi.fn(),
      url: new URL("https://jomcgi.dev/slop/factory/context"),
    });

    expect(result.projects.map((item) => item.slug)).toEqual(["embervm"]);
    expect(fetch).toHaveBeenCalledTimes(2);
    expect(result.facts).toEqual(facts);
  });

  it("loads a below-threshold entity chapter through its direct URL", async () => {
    const chapter = {
      entity: { slug: "small-project" },
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
      url: new URL(
        "https://jomcgi.dev/slop/factory/context?entity=small-project",
      ),
    });

    expect(fetch.mock.calls.map(([path]) => path)).toContain(
      "/slop/factory/entities/project/small-project/notes?state=verified%2Cunverified&limit=60",
    );
    expect(result.projects.map((project) => project.slug)).not.toContain(
      "small-project",
    );
    expect(result.entity).toBe("small-project");
    expect(result.chapter).toEqual(chapter);
  });

  it("caps grep searches and does not forward the removed page mode", async () => {
    const fetch = vi.fn(response);
    const result = await load({
      fetch,
      setHeaders: vi.fn(),
      url: new URL(
        `https://jomcgi.dev/slop/factory/context?q=${"x".repeat(220)}&mode=semantic`,
      ),
    });

    expect(result.q).toHaveLength(200);
    expect(result.mode).toBeUndefined();
    expect(fetch.mock.calls[2][0]).not.toContain("mode=");
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
        url: new URL(`https://jomcgi.dev/slop/factory/context${query}`),
      });

      expect(result.unavailable[section]).toBe(true);
      expect(result.title).toBe("Factory context");
    },
  );
});
