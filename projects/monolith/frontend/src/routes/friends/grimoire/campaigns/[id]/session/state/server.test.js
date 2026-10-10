import { beforeEach, describe, expect, it, vi } from "vitest";
import { GET, POST } from "./+server.js";

const campaignId = "11111111-1111-4111-8111-111111111111";
const sessionId = "33333333-3333-4333-8333-333333333333";
const pcA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const pcB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

function post(input) {
  const fetch = vi.fn(
    async () =>
      new Response(JSON.stringify({ id: "event", seq: 1 }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
  );
  const event = {
    fetch,
    cookies: { get: () => "signed-grimoire-token" },
    params: { id: campaignId },
    request: new Request("https://friends.jomcgi.dev/state", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ sessionId, ...input }),
    }),
  };
  return { fetch, response: POST(event) };
}

// What the backend received: the path and the parsed JSON body.
async function sent({ fetch, response }) {
  const result = await response;
  expect(result.status).toBe(200);
  expect(fetch).toHaveBeenCalledTimes(1);
  const [url, options] = fetch.mock.calls[0];
  expect(options.method).toBe("POST");
  return { url, body: JSON.parse(options.body) };
}

beforeEach(() => {
  process.env.API_BASE = "http://backend.test";
  process.env.GRIMOIRE_PLAY_ENABLED = "true";
});

describe("inventory proxy", () => {
  const base = `http://backend.test/api/grimoire/campaigns/${campaignId}/inventory`;
  const itemId = pcB;

  async function get(query) {
    const fetch = vi.fn(async () => new Response("[]"));
    const response = await GET({
      fetch,
      cookies: { get: () => "token" },
      params: { id: campaignId },
      url: new URL(`https://friends.jomcgi.dev/state?${query}`),
    });
    return { fetch, response };
  }

  it.each([
    ["inventory=items", base],
    ["inventory=changes", `${base}/changes`],
    [`inventory=changes&item=${itemId}`, `${base}/changes?item_id=${itemId}`],
  ])("reads %s privately", async (query, path) => {
    const { fetch, response } = await get(query);
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("private, no-store");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toBe(path);
    expect(fetch.mock.calls[0][1].method || "GET").toBe("GET");
  });

  it.each([
    "inventory=bad",
    "inventory=changes&item=bad",
    "inventory=changes&item=",
  ])("rejects invalid read %s", async (query) => {
    const { fetch, response } = await get(query);
    expect(response.status).toBe(400);
    expect(fetch).not.toHaveBeenCalled();
  });

  const give = {
    operation: "giveItem",
    owner: pcA,
    name: "Key",
    quantity: 2,
    notes: "Silver",
    entity_id: pcB,
    hidden_from_party: true,
    reason: "Reward",
    source_event_id: sessionId,
  };
  it.each([
    [
      give,
      "POST",
      base,
      {
        owner: pcA,
        name: "Key",
        quantity: 2,
        notes: "Silver",
        entity_id: pcB,
        hidden_from_party: true,
        reason: "Reward",
        source_event_id: sessionId,
      },
    ],
    [
      { operation: "updateItem", itemId, quantity: 0, reason: "Consumed" },
      "PATCH",
      `${base}/${itemId}`,
      { quantity: 0, reason: "Consumed" },
    ],
    [
      {
        operation: "updateItem",
        itemId,
        name: "Rope",
        notes: "Frayed",
        quantity: 10,
        hidden_from_party: false,
        entity_id: pcA,
        reason: "Correction",
      },
      "PATCH",
      `${base}/${itemId}`,
      {
        name: "Rope",
        notes: "Frayed",
        quantity: 10,
        hidden_from_party: false,
        entity_id: pcA,
        reason: "Correction",
      },
    ],
    [
      {
        operation: "moveItem",
        itemId,
        owner: "party",
        quantity: 1,
        reason: "Share",
      },
      "POST",
      `${base}/${itemId}/move`,
      { owner: "party", quantity: 1, reason: "Share" },
    ],
    [
      { operation: "moveItem", itemId, owner: pcA },
      "POST",
      `${base}/${itemId}/move`,
      { owner: pcA, reason: "" },
    ],
    [{ operation: "deleteItem", itemId }, "DELETE", `${base}/${itemId}`, {}],
  ])(
    "forwards $operation with exactly supplied fields",
    async (input, method, path, body) => {
      const request = post(input);
      expect((await request.response).status).toBe(200);
      expect(request.fetch).toHaveBeenCalledTimes(1);
      const [target, options] = request.fetch.mock.calls[0];
      expect(target).toBe(path);
      expect(options.method).toBe(method);
      expect(JSON.parse(options.body)).toEqual(body);
    },
  );

  it("handles a real no-content delete response", async () => {
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    const response = await POST({
      fetch,
      cookies: { get: () => "token" },
      params: { id: campaignId },
      request: new Request("https://friends.jomcgi.dev/state", {
        method: "POST",
        body: JSON.stringify({ operation: "deleteItem", itemId }),
      }),
    });
    expect(response.status).toBe(200);
  });

  it.each([
    { operation: "updateItem", itemId: "bad", quantity: 2 },
    { operation: "moveItem", itemId: "bad", owner: "party" },
    { operation: "deleteItem", itemId: "../inventory" },
    { operation: "deleteItem" },
    { operation: "deleteItem", itemId: [itemId] },
    { ...give, owner: "bad" },
    { ...give, owner: null },
    { ...give, owner: undefined },
    { ...give, owner: [pcA] },
    { operation: "moveItem", itemId, owner: "bad" },
    ...[null, "1", 0, -1, 1.5, 1000001].map((quantity) => ({
      ...give,
      quantity,
    })),
    ...[null, "1", -1, 1.5, 1000001].map((quantity) => ({
      operation: "updateItem",
      itemId,
      quantity,
    })),
    ...[null, 0, -1, 1.5, 1000001].map((quantity) => ({
      operation: "moveItem",
      itemId,
      owner: "party",
      quantity,
    })),
  ])(
    "rejects invalid mutation %# without contacting backend",
    async (input) => {
      const { fetch, response } = post(input);
      expect((await response).status).toBe(400);
      expect(fetch).not.toHaveBeenCalled();
    },
  );

  it("keeps both inventory reads and writes behind the play gate", async () => {
    process.env.GRIMOIRE_PLAY_ENABLED = "false";
    await expect(get("inventory=items")).rejects.toMatchObject({ status: 404 });
    await expect(post(give).response).rejects.toMatchObject({ status: 404 });
  });
});

describe("narration audience payload", () => {
  const eventsPath = `http://backend.test/api/grimoire/campaigns/${campaignId}/sessions/${sessionId}/events`;

  it("addresses the whole table with no PC list", async () => {
    const { url, body } = await sent(
      post({
        operation: "post",
        kind: "narration",
        text: "The lantern flickers.",
        audience: "table",
        pcIds: [],
      }),
    );
    expect(url).toBe(eventsPath);
    expect(body).toEqual({
      kind: "narration",
      audience: "table",
      audience_pc_ids: [],
      body: { text: "The lantern flickers." },
      request_id: null,
    });
  });

  it("keeps a DM note between the DM and the backend only", async () => {
    const { body } = await sent(
      post({
        operation: "post",
        kind: "narration",
        text: "Remember the trap on the stairs.",
        audience: "dm",
        requestId: "retry-1",
      }),
    );
    expect(body.audience).toBe("dm");
    expect(body.audience_pc_ids).toEqual([]);
    expect(body.request_id).toBe("retry-1");
    expect(body.body).toEqual({ text: "Remember the trap on the stairs." });
  });

  it("names exactly the chosen PCs for a private narration", async () => {
    const { body } = await sent(
      post({
        operation: "post",
        kind: "narration",
        text: "You alone notice the mark.",
        audience: "pcs",
        pcIds: [pcA, pcB],
      }),
    );
    expect(body.audience).toBe("pcs");
    expect(body.audience_pc_ids).toEqual([pcA, pcB]);
  });

  it("carries a private reply's thread and resolution for narration only", async () => {
    const reply = {
      operation: "post",
      text: "The door gives way.",
      audience: "pcs",
      pcIds: [pcA],
      replyTo: "action-event",
      resolved: true,
    };
    const { body: dm } = await sent(post({ ...reply, kind: "narration" }));
    expect(dm.body).toEqual({
      text: "The door gives way.",
      reply_to: "action-event",
      resolved: true,
    });
    const { body: player } = await sent(post({ ...reply, kind: "action" }));
    expect(player.body).toEqual({ text: "The door gives way." });
    expect(player.audience_pc_ids).toEqual([pcA]);
  });

  it("sends a player's private action to the DM audience", async () => {
    const { body } = await sent(
      post({
        operation: "post",
        kind: "action",
        text: "I pocket the key.",
        audience: "dm",
        pcIds: [],
      }),
    );
    expect(body).toMatchObject({
      kind: "action",
      audience: "dm",
      audience_pc_ids: [],
    });
  });

  it("rejects an empty message before contacting the backend", async () => {
    const { fetch, response } = post({
      operation: "post",
      kind: "narration",
      text: "   ",
      audience: "table",
    });
    const result = await response;
    expect(result.status).toBe(400);
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("reveal payload", () => {
  const bulkPath = `http://backend.test/api/grimoire/campaigns/${campaignId}/grants/bulk`;
  const reveal = (extra) => ({
    operation: "reveal",
    entityId: pcB,
    pcIds: [pcA],
    ...extra,
  });

  it("forwards exactly the picked keys as revealed_details", async () => {
    const { url, body } = await sent(
      post(
        reveal({
          scope: "partial",
          revealedDetails: { occupation: "Smuggler" },
          clue: "ignored when keys are picked",
        }),
      ),
    );
    expect(url).toBe(bulkPath);
    expect(body.grants).toEqual([
      {
        entity_id: pcB,
        player_character_id: pcA,
        grant_scope: "partial",
        revealed_details: { occupation: "Smuggler" },
      },
    ]);
  });

  it("sends no details for a full grant", async () => {
    const { body } = await sent(
      post(reveal({ scope: "full", revealedDetails: { occupation: "x" } })),
    );
    expect(body.grants[0].revealed_details).toBeNull();
  });
});
