import { afterEach, expect, it, vi } from "vitest";
import { load as entities } from "./entities/+page.server.js";
import { load as explore } from "./explore/+page.server.js";
import { load as chunk } from "./book/[book]/c/[chunk]/+page.server.js";
import { load as book } from "./book/[book]/+page.server.js";

afterEach(() => vi.unstubAllEnvs());
it("keeps Entities a plain permanent World redirect", () => {
  expect(() => entities()).toThrow(
    expect.objectContaining({ status: 301, location: "/app/grimoire/world" }),
  );
});
it("maps legacy Explore focus, scope and lens without retaining obsolete search", () => {
  expect(() =>
    explore({
      url: new URL(
        "https://example.test/app/grimoire/explore?focus=fixture%20person&scope=fixture&lens=story&q=ignored",
      ),
    }),
  ).toThrow(
    expect.objectContaining({
      status: 301,
      location: "/app/grimoire/world?e=fixture+person&scope=fixture&lens=story",
    }),
  );
  expect(() =>
    explore({
      url: new URL(
        "https://example.test/app/grimoire/explore?scope=everything&lens=world",
      ),
    }),
  ).toThrow(
    expect.objectContaining({ status: 301, location: "/app/grimoire/world" }),
  );
});
it("keeps chunk cursor and fragment, including the locked-book redirect", async () => {
  vi.stubEnv("API_BASE", "http://fixture.invalid");
  const params = { book: "fixture-book", chunk: "fixture-chunk" };
  await expect(
    chunk({
      params,
      fetch: vi.fn().mockResolvedValue(Response.json({ seq: 8 })),
    }),
  ).rejects.toMatchObject({
    status: 307,
    location: "/app/grimoire/book/fixture-book?from=7#c-fixture-chunk",
  });
  await expect(
    chunk({
      params,
      fetch: vi.fn().mockResolvedValue(new Response(null, { status: 403 })),
    }),
  ).rejects.toMatchObject({
    status: 307,
    location: "/app/grimoire/book/fixture-book",
  });
});
it("forwards reader cursors and preserves locked metadata", async () => {
  vi.stubEnv("API_BASE", "http://fixture.invalid");
  const fetch = vi
    .fn()
    .mockResolvedValue(Response.json({ items: [], next_cursor: "next" }));
  const args = {
    params: { book: "fixture-book" },
    url: new URL(
      "https://example.test/app/grimoire/book/fixture-book?from=a%2Fb",
    ),
    fetch,
  };
  expect(await book(args)).toEqual({
    bookId: "fixture-book",
    locked: false,
    items: [],
    nextCursor: "next",
  });
  expect(fetch.mock.calls[0][0]).toBe(
    "http://fixture.invalid/api/grimoire/books/fixture-book/read?cursor=a%2Fb",
  );
  fetch.mockResolvedValue(new Response(null, { status: 403 }));
  expect(await book(args)).toEqual({
    bookId: "fixture-book",
    locked: true,
    items: [],
    nextCursor: null,
  });
});
