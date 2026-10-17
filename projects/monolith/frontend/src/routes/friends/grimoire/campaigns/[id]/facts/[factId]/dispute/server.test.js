import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { POST } from "./+server.js";

const id = "11111111-1111-4111-8111-111111111111";
const factId = "22222222-2222-4222-8222-222222222222";
const event = (fetch, params = { id, factId }) => ({
  fetch,
  params,
  cookies: { get: () => "signed-token" },
});
beforeEach(() => vi.stubEnv("API_BASE", "http://backend.test"));
afterEach(() => vi.unstubAllEnvs());

it("forwards only the authenticated POST and prevents caching", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValue(
      new Response(JSON.stringify({ id: factId, status: "disputed" })),
    );
  const result = await POST(event(fetch));
  expect(result.status).toBe(200);
  expect(result.headers.get("cache-control")).toBe("private, no-store");
  expect(fetch).toHaveBeenCalledWith(
    `http://backend.test/api/grimoire/campaigns/${id}/facts/${factId}/dispute`,
    expect.objectContaining({
      method: "POST",
      headers: { "x-grimoire-token": "signed-token" },
    }),
  );
});
it.each([
  { id: "../foreign", factId },
  { id, factId: "not-an-id" },
])("rejects invalid identifiers before forwarding", async (params) => {
  const fetch = vi.fn();
  const result = await POST(event(fetch, params));
  expect(result.status).toBe(404);
  expect(fetch).not.toHaveBeenCalled();
});
it("never reflects private backend errors", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValue(
      new Response(JSON.stringify({ detail: "OTHER-PC-SECRET" }), {
        status: 404,
      }),
    );
  const result = await POST(event(fetch));
  expect(result.status).toBe(404);
  expect(await result.text()).not.toContain("OTHER-PC-SECRET");
});
