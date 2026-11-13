import { expect, it, vi } from "vitest";
import { GET } from "./+server.js";
it("proxies history through same origin with backend ETag and deadline bound", async () => {
  const payload = {
    review_after: "2099-01-01T00:00:00Z",
    review_history: { entries: [], has_more: false },
  };
  const fetch = vi.fn(
    async () =>
      new Response(JSON.stringify(payload), {
        headers: {
          etag: '\"history-v1\"',
          "cache-control": "public, max-age=7, s-maxage=7, must-revalidate",
        },
      }),
  );
  const setHeaders = vi.fn();
  const response = await GET({ params: { id: "a/b" }, fetch, setHeaders });
  expect(fetch.mock.calls[0][0]).toContain("/api/knowledge/public/notes/a%2Fb");
  expect(await response.json()).toEqual(payload);
  const headers = setHeaders.mock.calls[0][0];
  expect(headers.etag).toContain("history-v1");
  expect(headers["cache-control"]).toContain("s-maxage=7");
  expect(headers["cache-control"]).not.toContain("stale-");
});
