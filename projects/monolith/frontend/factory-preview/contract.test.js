import { afterAll, describe, expect, it } from "vitest";
import { mkdirSync, writeFileSync } from "node:fs";
import { load as overview } from "../src/routes/public/slop/factory/+page.server.js";
import { load as activity } from "../src/routes/public/slop/factory/activity/+page.server.js";
import { load as context } from "../src/routes/public/slop/factory/context/+page.server.js";
import { ledger, ledgerMeta } from "../src/lib/public/factory/activity-view.js";
import { decodeSearchIndex } from "../src/lib/public/factory/search-index.js";
import { NOW, payloads, LONG_TITLE } from "./fixtures.js";

const generated = {};
const views = {
  overview: [overview, "/slop/factory"],
  activity: [activity, "/slop/factory/activity"],
  context: [context, "/slop/factory/context"],
  chapter: [context, "/slop/factory/context?entity=synthetic-project"],
  search: [context, "/slop/factory/context?q=Synthetic"],
};
describe("same-commit factory server-load fixtures", () => {
  for (const scenario of ["live", "empty", "error"]) {
    for (const [view, [load, path]] of Object.entries(views)) {
      it(`${scenario} ${view} only reads invented fixture endpoints`, async () => {
        const endpoints = payloads(scenario);
        const unexpected = [];
        let headers;
        const data = await load({
          url: new URL(path, "https://fixture.invalid"),
          setHeaders: (value) => {
            headers = value;
          },
          fetch: async (url) => {
            if (!(url in endpoints)) {
              unexpected.push(url);
              throw new Error(`No synthetic fixture for ${url}`);
            }
            return new Response(JSON.stringify(endpoints[url]), {
              status: scenario === "error" ? 503 : 200,
              headers: {
                "content-type": "application/json",
                etag: '"synthetic"',
              },
            });
          },
        });
        expect(unexpected).toEqual([]);
        expect(headers["cache-control"]).toContain("public");
        if (scenario === "error") {
          expect(
            typeof data.unavailable === "boolean"
              ? data.unavailable
              : Object.values(data.unavailable).some(Boolean),
          ).toBe(true);
        } else if (view === "overview" || view === "activity") {
          const book = ledger(data.board, NOW);
          expect(book.live).toHaveLength(scenario === "live" ? 2 : 0);
          if (scenario === "live") {
            expect(book.live[0].title).toBe(LONG_TITLE);
            expect(ledgerMeta(book.live[0], data.board.policy, NOW)).toContain(
              "review",
            );
            expect(book.done).toHaveLength(25);
          }
        } else if (view === "chapter" && scenario === "live") {
          expect(data.projects).toHaveLength(1);
          expect(data.chapter.notes).toHaveLength(26);
          expect(data.chapter.notes[0].title).toBe(LONG_TITLE);
        } else if (view === "search" && scenario === "live") {
          expect(data.results).toHaveLength(26);
        }
        generated[`${scenario}/${view}`] = data;
      });
    }
  }
  it("instant-search fixtures use the production decoder", () => {
    expect(
      decodeSearchIndex(payloads()["/slop/factory/search-index"]),
    ).toHaveLength(26);
  });
});

// The browser artifact consumes the actual loaders' return values. This is
// generated locally/CI, never a hand-maintained snapshot of production data.
afterAll(() => {
  expect(Object.keys(generated)).toHaveLength(15);
  const directory = new URL("./.generated/", import.meta.url);
  mkdirSync(directory, { recursive: true });
  writeFileSync(new URL("pages.json", directory), JSON.stringify(generated));
});
