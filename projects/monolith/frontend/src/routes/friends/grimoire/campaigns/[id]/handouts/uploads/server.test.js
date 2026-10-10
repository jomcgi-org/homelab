import { beforeEach, describe, expect, it, vi } from "vitest";
import { POST } from "./+server.js";
import { HANDOUT_IMAGE_MAX_BYTES } from "$lib/grimoire/handout.js";

const campaignId = "11111111-1111-4111-8111-111111111111";
const png = Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function upload({ file, headers = {}, field = "file", id = campaignId } = {}) {
  const form = new FormData();
  if (file) form.set(field, file, "../../evil.png");
  const fetch = vi.fn(
    async () =>
      new Response(
        JSON.stringify({ key: "k.png", content_type: "image/png", size: 8 }),
        { status: 201, headers: { "content-type": "application/json" } },
      ),
  );
  return {
    fetch,
    response: POST({
      fetch,
      cookies: { get: () => "signed-grimoire-token" },
      params: { id },
      request: new Request("https://friends.jomcgi.dev/upload", {
        method: "POST",
        body: form,
        headers,
      }),
    }),
  };
}

beforeEach(() => {
  process.env.API_BASE = "http://backend.test";
  process.env.GRIMOIRE_PLAY_ENABLED = "true";
});

describe("handout upload proxy", () => {
  it("forwards the file with the member token to the backend upload route", async () => {
    const { fetch, response } = upload({
      file: new Blob([png], { type: "text/plain" }),
    });
    const result = await response;
    expect(result.status).toBe(201);
    expect(await result.json()).toEqual({
      key: "k.png",
      content_type: "image/png",
      size: 8,
    });
    const [url, options] = fetch.mock.calls[0];
    expect(url).toBe(
      `http://backend.test/api/grimoire/campaigns/${campaignId}/handouts/uploads`,
    );
    expect(options.method).toBe("POST");
    expect(options.headers).toEqual({ "x-grimoire-token": "signed-grimoire-token" });
    const sent = options.body.get("file");
    expect(sent.size).toBe(png.length);
    // The client's filename never reaches the backend.
    expect(sent.name).toBe("handout");
  });

  it("rejects an oversize declared content-length before reading the body", async () => {
    const { fetch, response } = upload({
      file: new Blob([png]),
      headers: { "content-length": String(HANDOUT_IMAGE_MAX_BYTES * 2) },
    });
    expect((await response).status).toBe(413);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("rejects an oversize file when no length was declared", async () => {
    const { fetch, response } = upload({
      file: new Blob([new Uint8Array(HANDOUT_IMAGE_MAX_BYTES + 1)]),
    });
    expect((await response).status).toBe(413);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("rejects a request without a file", async () => {
    const { fetch, response } = upload({
      file: new Blob([png]),
      field: "other",
    });
    expect((await response).status).toBe(400);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("passes the backend refusal status and detail through", async () => {
    const form = new FormData();
    form.set("file", new Blob(["not an image"]), "x.txt");
    const fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ detail: "unsupported image type" }), {
          status: 415,
          headers: { "content-type": "application/json" },
        }),
    );
    const result = await POST({
      fetch,
      cookies: { get: () => "t" },
      params: { id: campaignId },
      request: new Request("https://friends.jomcgi.dev/upload", {
        method: "POST",
        body: form,
      }),
    });
    expect(result.status).toBe(415);
    expect(await result.json()).toEqual({ error: "unsupported image type" });
  });

  it("refuses a campaign id that is not a single uuid segment", async () => {
    const { fetch, response } = upload({
      file: new Blob([png]),
      id: "../../admin",
    });
    await expect(response).rejects.toMatchObject({ status: 404 });
    expect(fetch).not.toHaveBeenCalled();
  });

  it("is not served while play is disabled", async () => {
    process.env.GRIMOIRE_PLAY_ENABLED = "false";
    const { response } = upload({ file: new Blob([png]) });
    await expect(response).rejects.toMatchObject({ status: 404 });
  });
});
