import { beforeEach, describe, expect, it, vi } from "vitest";
import { POST } from "./+server.js";

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
