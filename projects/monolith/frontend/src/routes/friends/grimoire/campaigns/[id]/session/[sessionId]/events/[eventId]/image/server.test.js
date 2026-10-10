import { beforeEach, describe, expect, it, vi } from "vitest";
import { GET } from "./+server.js";

const campaignId = "11111111-1111-4111-8111-111111111111";
const sessionId = "33333333-3333-4333-8333-333333333333";
const eventId = "55555555-5555-4555-8555-555555555555";

function get({ upstream, params } = {}) {
  const fetch = vi.fn(async () => upstream);
  return {
    fetch,
    response: GET({
      fetch,
      cookies: { get: () => "signed-grimoire-token" },
      params: { id: campaignId, sessionId, eventId, ...params },
    }),
  };
}

beforeEach(() => {
  process.env.API_BASE = "http://backend.test";
  process.env.GRIMOIRE_PLAY_ENABLED = "true";
});

describe("handout image proxy", () => {
  it("streams the backend image with the member token and no caching", async () => {
    const bytes = Uint8Array.from([1, 2, 3, 4]);
    const { fetch, response } = get({
      upstream: new Response(bytes, {
        status: 200,
        headers: { "content-type": "image/png", "cache-control": "max-age=99" },
      }),
    });
    const result = await response;
    expect(result.status).toBe(200);
    expect(result.headers.get("content-type")).toBe("image/png");
    expect(result.headers.get("cache-control")).toBe("private, no-store");
    expect(new Uint8Array(await result.arrayBuffer())).toEqual(bytes);
    const [url, options] = fetch.mock.calls[0];
    expect(url).toBe(
      `http://backend.test/api/grimoire/campaigns/${campaignId}/sessions/${sessionId}/events/${eventId}/image`,
    );
    expect(options.headers).toEqual({ "x-grimoire-token": "signed-grimoire-token" });
  });

  it("passes a backend 404 through unchanged, still uncached", async () => {
    const { response } = get({
      upstream: new Response(JSON.stringify({ detail: "handout image not found" }), {
        status: 404,
        headers: { "content-type": "application/json" },
      }),
    });
    const result = await response;
    expect(result.status).toBe(404);
    expect(result.headers.get("cache-control")).toBe("private, no-store");
    expect(await result.json()).toEqual({ detail: "handout image not found" });
  });

  it.each(["id", "sessionId", "eventId"])(
    "refuses a %s that is not a single uuid segment",
    async (name) => {
      const { fetch, response } = get({ params: { [name]: "../../x" } });
      await expect(response).rejects.toMatchObject({ status: 404 });
      expect(fetch).not.toHaveBeenCalled();
    },
  );

  it("is not served while play is disabled", async () => {
    process.env.GRIMOIRE_PLAY_ENABLED = "false";
    const { fetch, response } = get();
    await expect(response).rejects.toMatchObject({ status: 404 });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("answers 401 without a grimoire token", async () => {
    const fetch = vi.fn();
    await expect(
      GET({
        fetch,
        cookies: { get: () => undefined },
        params: { id: campaignId, sessionId, eventId },
      }),
    ).rejects.toMatchObject({ status: 401 });
    expect(fetch).not.toHaveBeenCalled();
  });
});
