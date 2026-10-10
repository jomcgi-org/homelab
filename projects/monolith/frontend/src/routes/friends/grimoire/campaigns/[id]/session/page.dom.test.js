// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Page from "./+page.svelte";

const campaignId = "11111111-1111-4111-8111-111111111111";
const sessionId = "33333333-3333-4333-8333-333333333333";
const pcA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const pcB = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const endpoint = `/grimoire/campaigns/${campaignId}/session/state`;

function event(id, seq, text, overrides = {}) {
  return {
    id,
    seq,
    kind: "narration",
    audience: "table",
    audience_pc_ids: [],
    author_member_id: "member-dm",
    body: { text },
    created_at: "2026-10-10T12:00:00Z",
    retracted_at: null,
    ...overrides,
  };
}

// The BFF projects a retraction as the same event, by id and seq, with
// `retracted_at` set and the body withheld from players.
function retracted(row) {
  return { ...row, body: null, retracted_at: "2026-10-10T12:05:00Z" };
}

function pageData(role, events) {
  return {
    campaign: { id: campaignId, name: "Adventure", role },
    characters: [
      { id: pcA, character_name: "Aria", approved: null },
      { id: pcB, character_name: "Bram", approved: null },
    ],
    session: { id: sessionId, status: "active" },
    events,
    journal: null,
    user: { id: "viewer" },
    ...(role === "dm"
      ? { members: [{ id: "member-a", player_character_id: pcA }] }
      : {}),
  };
}

const json = (value) =>
  new Response(JSON.stringify(value), {
    status: 200,
    headers: { "content-type": "application/json" },
  });

// Routes the page's traffic: notes polls get an empty list, posts echo an
// id, and each state poll gets whatever `nextState` returns at that moment.
function stubFetch(nextState) {
  const fetch = vi.fn(async (url, options = {}) => {
    if (String(url).includes("?notes=")) return json([]);
    if (options.method === "POST") return json({ id: "posted", seq: 99 });
    return json(nextState());
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

const feedIds = () =>
  [...document.querySelectorAll(".feed article")].map((node) => node.id);
const statePolls = (fetch) =>
  fetch.mock.calls.filter(
    ([url, options]) => url === endpoint && !options?.method,
  );
const posted = (fetch) =>
  fetch.mock.calls
    .filter(([url, options]) => url === endpoint && options?.method === "POST")
    .map(([, options]) => JSON.parse(options.body));

async function settle(times = 6) {
  for (let step = 0; step < times; step += 1) await tick();
}

let instance;
async function render(data) {
  instance = mount(Page, { target: document.body, props: { data } });
  await tick();
}
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("session feed polling", () => {
  it("merges overlapping polls without duplicates and projects a retraction", async () => {
    vi.useFakeTimers();
    const held = [
      event("a", 1, "The lantern flickers."),
      event("b", 2, "A voice calls your name."),
      event("c", 3, "The door creaks open."),
    ];
    // Both polls overlap everything already held and add one event; the
    // second also carries b's retraction.
    const overlap = [...held, event("d", 4, "Footsteps approach.")];
    const withRetraction = [held[0], retracted(held[1]), held[2], overlap[3]];
    const pages = [overlap, withRetraction];
    const fetch = stubFetch(() =>
      pageData("player", pages.shift() ?? withRetraction),
    );
    await render(pageData("player", held));
    expect(feedIds()).toEqual(["event-a", "event-b", "event-c"]);

    // The scheduled poll and a visibility refresh land back to back.
    await vi.advanceTimersByTimeAsync(2000);
    document.dispatchEvent(new Event("visibilitychange"));
    await settle();
    expect(statePolls(fetch)).toHaveLength(2);
    expect(feedIds()).toEqual(["event-a", "event-b", "event-c", "event-d"]);

    const feed = document.querySelector(".feed");
    expect(feed.textContent).toContain("This message was retracted.");
    expect(feed.textContent).not.toContain("A voice calls your name.");
    expect(feed.textContent).toContain("Footsteps approach.");
    // A retracted event offers nothing to pin.
    expect(document.getElementById("event-b").querySelector("button")).toBe(
      null,
    );
    expect(document.querySelector('[role="status"]').textContent).toBe("Live");

    // Re-applying the same page changes nothing.
    await vi.advanceTimersByTimeAsync(2000);
    await settle();
    expect(statePolls(fetch)).toHaveLength(3);
    expect(feedIds()).toEqual(["event-a", "event-b", "event-c", "event-d"]);
    expect(feed.textContent.match(/This message was retracted\./g)).toEqual([
      "This message was retracted.",
    ]);
  });

  it("keeps the held feed and reports the outage when a poll fails", async () => {
    vi.useFakeTimers();
    stubFetch(() => {
      throw new TypeError("Failed to fetch");
    });
    await render(pageData("player", [event("a", 1, "The lantern flickers.")]));
    await vi.advanceTimersByTimeAsync(2000);
    await settle();
    expect(feedIds()).toEqual(["event-a"]);
    expect(document.querySelector(".feed").textContent).toContain(
      "The lantern flickers.",
    );
    expect(document.querySelector('[role="status"]').textContent).toBe(
      "Reconnecting",
    );
  });
});

describe("audience picker", () => {
  const options = () =>
    [...document.querySelector('select[aria-label="Send to"]').options].map(
      (option) => [option.value, option.textContent],
    );

  // happy-dom cannot drive a bound <select> (its `:checked` ignores
  // options), so the mapping of each choice is covered by
  // session-compose.test.js; these cases submit the real form on the
  // default choice and on the private-reply path, which picks a PC itself.
  async function send(text) {
    const textarea = document.getElementById("message");
    textarea.value = text;
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    await tick();
    document
      .querySelector("form.composer")
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await settle();
  }

  it("offers the DM the table, a DM note and each PC, and posts narration", async () => {
    const privateAction = event("act", 1, "I pocket the key.", {
      kind: "action",
      audience: "dm",
      author_member_id: "member-a",
    });
    const fetch = stubFetch(() => pageData("dm", [privateAction]));
    await render(pageData("dm", [privateAction]));
    expect(options()).toEqual([
      ["table", "Everyone"],
      ["dm", "DM notes"],
      [`pc:${pcA}`, "Aria and DM"],
      [`pc:${pcB}`, "Bram and DM"],
    ]);

    await send("The lantern flickers.");
    expect(posted(fetch)).toEqual([
      expect.objectContaining({
        sessionId,
        operation: "post",
        kind: "narration",
        text: "The lantern flickers.",
        audience: "table",
        pcIds: [],
        resolved: true,
      }),
    ]);
    expect(posted(fetch)[0]).not.toHaveProperty("replyTo");
    expect(posted(fetch)[0].requestId).toMatch(/^[0-9a-f-]{36}$/);

    // Replying privately addresses exactly that player's character.
    [...document.querySelectorAll("button")]
      .find((button) => button.textContent.trim() === "Reply privately to Aria")
      .click();
    await tick();
    expect(document.querySelector('select[aria-label="Send to"]').value).toBe(
      `pc:${pcA}`,
    );
    await send("The key is warm to the touch.");
    const reply = posted(fetch)[1];
    expect(reply).toMatchObject({
      kind: "narration",
      text: "The key is warm to the touch.",
      audience: "pcs",
      pcIds: [pcA],
      replyTo: "act",
      resolved: true,
    });
    expect(reply.requestId).not.toBe(posted(fetch)[0].requestId);
  });

  it("gives a player the table and the DM only, with no session controls", async () => {
    const fetch = stubFetch(() => pageData("player", []));
    await render(pageData("player", []));
    expect(options()).toEqual([
      ["table", "Everyone"],
      ["dm", "DM privately"],
    ]);
    const labels = [...document.querySelectorAll("button")].map((button) =>
      button.textContent.trim(),
    );
    for (const control of [
      "Start session",
      "Pause session",
      "Resume session",
      "End session",
    ])
      expect(labels).not.toContain(control);
    expect(document.body.textContent).not.toContain("Set the scene");
    expect(document.body.textContent).not.toContain("Manage knowledge grants");

    await send("I approach the door.");
    expect(posted(fetch)).toEqual([
      expect.objectContaining({
        kind: "action",
        audience: "table",
        pcIds: [],
        text: "I approach the door.",
      }),
    ]);
  });
});
