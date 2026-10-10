// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Page from "./+page.svelte";
import { load } from "./+page.server.js";
import { GET } from "./state/+server.js";

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

function pageData(role, events, { journal = null, session } = {}) {
  return {
    campaign: {
      id: campaignId,
      name: "Adventure",
      role,
      player_character_id: role === "dm" ? null : pcA,
    },
    characters: [
      { id: pcA, character_name: "Aria", approved: null },
      { id: pcB, character_name: "Bram", approved: null },
    ],
    session:
      session === undefined ? { id: sessionId, status: "active" } : session,
    events,
    journal,
    voices: [],
    user: { id: "viewer" },
    ...(role === "dm"
      ? {
          members: [{ id: "member-a", player_character_id: pcA }],
          npcs: [
            {
              id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
              name: "NPC_CANARY",
              entity_type: "npc",
            },
          ],
        }
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
    if (String(url).includes("?notes=") || String(url).includes("?inventory="))
      return json([]);
    if (String(url).includes("?entity="))
      return json({ id: "entity-mara", name: "Mara", entity_type: "npc" });
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
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.useRealTimers();
});

describe("session inventory", () => {
  const entityId = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee";
  const knowledge = {
    entity_id: entityId,
    name: "Silver key",
    grant_scope: "name_only",
  };
  const reveal = event("44444444-4444-4444-8444-444444444444", 1, "", {
    kind: "reveal",
    audience: "pcs",
    audience_pc_ids: [pcA],
    body: { reveals: [knowledge] },
  });
  const handout = event(
    "55555555-5555-4555-8555-555555555555",
    2,
    "Ancient map\nWith a marked road",
    { kind: "handout" },
  );
  const system = event(
    "66666666-6666-4666-8666-666666666666",
    3,
    "Inventory changed in the party pool.",
    { kind: "system" },
  );
  const click = async (text) => {
    const button = [...document.querySelectorAll("button")].find(
      (node) => node.textContent.trim() === text,
    );
    expect(button, text).toBeTruthy();
    button.click();
    await settle();
  };
  const field = (text) =>
    [...document.querySelectorAll(".inventory label")]
      .find(
        (node) =>
          [...node.childNodes]
            .filter((child) => child.nodeType === 3)
            .map((child) => child.textContent)
            .join("")
            .trim() === text,
      )
      ?.querySelector("input, select");

  it("toggles the Inventory tab after Notes and back to Story", async () => {
    const data = pageData("player", []);
    stubFetch(() => data);
    await render(data);
    expect(
      [...document.querySelectorAll(".table-tabs button")].map((node) =>
        node.textContent.trim(),
      ),
    ).toEqual(["Story", "Notes", "Inventory", "Journal"]);
    await click("Inventory");
    expect(
      document.querySelector('[aria-label="Campaign inventory"]'),
    ).toBeTruthy();
    expect(
      document.querySelector('.table-tabs button[aria-pressed="true"]')
        .textContent,
    ).toBe("Inventory");
    await click("Story");
    expect(
      document.querySelector('[aria-label="Campaign inventory"]'),
    ).toBeNull();
    expect(
      document.querySelector('.table-tabs button[aria-pressed="true"]')
        .textContent,
    ).toBe("Story");
  });

  it("prefills and posts a reveal to its one addressed PC with entity and source event", async () => {
    const data = pageData("dm", [reveal, handout, system]);
    const fetch = stubFetch(() => data);
    await render(data);
    document
      .querySelector(
        `#event-${reveal.id} button[aria-label="Give item Silver key"]`,
      )
      .click();
    await settle();
    expect(field("Item name").value).toBe("Silver key");
    expect(field("Give to").value).toBe(pcA);
    await click("Give Silver key");
    expect(posted(fetch)).toEqual([
      {
        operation: "giveItem",
        owner: pcA,
        name: "Silver key",
        quantity: 1,
        notes: "",
        hidden_from_party: false,
        reason: "",
        entity_id: entityId,
        source_event_id: reveal.id,
      },
    ]);
  });

  it.each([
    ["table", []],
    ["pcs", [pcA, pcB]],
    ["dm", []],
  ])(
    "prefills a %s reveal to the pool when there is no single PC",
    async (audience, audience_pc_ids) => {
      const row = { ...reveal, audience, audience_pc_ids };
      const data = pageData("dm", [row]);
      stubFetch(() => data);
      await render(data);
      await click("Give item");
      expect(field("Give to").value).toBe("party");
    },
  );

  it("prefills a handout's truncated first line for the pool without an entity", async () => {
    const row = {
      ...handout,
      body: { text: `${"a".repeat(240)}\nSecond line` },
    };
    const data = pageData("dm", [row]);
    const fetch = stubFetch(() => data);
    await render(data);
    await click("Give item");
    expect(field("Item name").value).toBe("a".repeat(200));
    expect(field("Give to").value).toBe("party");
    await click(`Give ${"a".repeat(200)}`);
    expect(posted(fetch)[0]).toMatchObject({
      operation: "giveItem",
      name: "a".repeat(200),
      owner: "party",
      entity_id: null,
      source_event_id: handout.id,
    });
  });

  it("labels system and handout events and never offers players Give item", async () => {
    const data = pageData("player", [reveal, handout, system]);
    stubFetch(() => data);
    await render(data);
    expect(
      document.querySelector(`#event-${handout.id} .event-meta strong`)
        .textContent,
    ).toBe("Handout");
    expect(
      document.querySelector(`#event-${system.id} .event-meta strong`)
        .textContent,
    ).toBe("Table update");
    expect(
      [...document.querySelectorAll("button")].some(
        (node) => node.textContent === "Give item",
      ),
    ).toBe(false);
  });

  it("offers no give for retracted events, entries or removed reveal entities", async () => {
    const data = pageData("dm", [
      retracted(reveal),
      retracted(handout),
      {
        ...reveal,
        id: "retracted-entry",
        seq: 3,
        body: { ...knowledge, retracted: true },
      },
      {
        ...reveal,
        id: "removed-entry",
        seq: 4,
        body: { reveals: [knowledge], retracted_entity_ids: [entityId] },
      },
    ]);
    stubFetch(() => data);
    await render(data);
    expect(
      [...document.querySelectorAll("button")].some(
        (node) => node.textContent === "Give item",
      ),
    ).toBe(false);
  });
});

describe("session read-aloud controls and voice composer", () => {
  function speechDevice() {
    const synth = {
      speak: vi.fn(),
      cancel: vi.fn(),
      getVoices: () => [],
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    };
    vi.stubGlobal("speechSynthesis", synth);
    vi.stubGlobal(
      "SpeechSynthesisUtterance",
      class {
        constructor(text) {
          this.text = text;
        }
      },
    );
    return synth;
  }
  const checkbox = (label) =>
    [...document.querySelectorAll("label")]
      .find((node) => node.textContent.trim() === label)
      ?.querySelector("input");
  async function choose(select, value) {
    // happy-dom's :checked selector omits options. Supply the selected option
    // for Svelte's binding while retaining the real change listener and form.
    const query = select.querySelector.bind(select);
    vi.spyOn(select, "querySelector").mockImplementation((selector) =>
      selector === ":checked"
        ? [...select.options].find((option) => option.value === select.value)
        : query(selector),
    );
    select.value = value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    await tick();
  }

  it("renders the DM speaker picker and editor, sends NPC and free-label narration including replies", async () => {
    const data = pageData("dm", [
      event("action", 1, "Hello", {
        kind: "action",
        audience: "dm",
        author_member_id: "member-a",
      }),
    ]);
    const fetch = stubFetch(() => data);
    await render(data);
    const select = document.querySelector('[aria-label="Narration speaker"]');
    expect([...select.options].map((option) => option.text)).toEqual([
      "Narrator",
      "NPC_CANARY",
      "Free label",
    ]);
    expect(document.querySelector(".voice-presets")).not.toBeNull();
    await choose(select, data.npcs[0].id);
    document.querySelector("#message").value = "The captain speaks.";
    document
      .querySelector("#message")
      .dispatchEvent(new Event("input", { bubbles: true }));
    document
      .querySelector(".composer")
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await settle();
    expect(posted(fetch)[0]).toMatchObject({
      kind: "narration",
      speakerKey: data.npcs[0].id,
    });
    const replyButton = [...document.querySelectorAll("button")].find(
      (button) => button.textContent.includes("Reply privately to Aria"),
    );
    replyButton.click();
    await settle();
    await choose(select, "custom");
    const label = document.querySelector('[aria-label="Speaker label"]');
    label.value = "Captain North";
    label.dispatchEvent(new Event("input", { bubbles: true }));
    document.querySelector("#message").value = "You hear a whisper.";
    document
      .querySelector("#message")
      .dispatchEvent(new Event("input", { bubbles: true }));
    document
      .querySelector(".composer")
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await settle();
    expect(posted(fetch)[1]).toMatchObject({
      speakerKey: "Captain North",
      replyTo: "action",
      audience: "pcs",
      pcIds: [pcA],
    });
  });

  it("players never render the picker, editor or names from voice keys", async () => {
    speechDevice();
    const data = {
      ...pageData("player", []),
      voices: [
        {
          speaker_key: "ref:0123456789abcdef0123",
          voice_hint: { names: ["NPC_CANARY"] },
        },
      ],
    };
    stubFetch(() => data);
    await render(data);
    expect(
      document.querySelector('[aria-label="Narration speaker"]'),
    ).toBeNull();
    expect(document.querySelector(".voice-presets")).toBeNull();
    expect(document.body.textContent).not.toContain("NPC_CANARY");
    expect(document.body.textContent).not.toContain("ref:");
    expect(checkbox("Read DM narration").checked).toBe(false);
    expect(checkbox("Read my reveals and private narration").checked).toBe(
      false,
    );
  });

  it("skips backlog, speaks new table narration by default on DM refresh, and stops", async () => {
    vi.useFakeTimers();
    const synth = speechDevice();
    let data = pageData("dm", [event("backlog", 1, "Old story")]);
    stubFetch(() => data);
    await render(data);
    await settle();
    expect(synth.speak).not.toHaveBeenCalled();
    expect(checkbox("Read DM narration").checked).toBe(true);
    expect(checkbox("Read my reveals and private narration")).toBeUndefined();
    data = pageData("dm", [
      ...data.events,
      event("new", 2, "New story"),
      event("private", 3, "PRIVATE_CANARY", {
        audience: "pcs",
        audience_pc_ids: [pcA],
      }),
    ]);
    await vi.advanceTimersByTimeAsync(2000);
    await settle();
    expect(synth.speak).toHaveBeenCalledTimes(1);
    expect(synth.speak.mock.calls[0][0].text).toBe("New story");
    const stop = [...document.querySelectorAll("button")].find(
      (button) => button.textContent === "Stop read-aloud",
    );
    expect(stop.disabled).toBe(false);
    stop.click();
    await tick();
    expect(stop.disabled).toBe(true);
    expect(synth.cancel).toHaveBeenCalledTimes(2);
    checkbox("Read DM narration").click();
    await tick();
    expect(
      JSON.parse(localStorage.getItem(`grimoire:read-aloud:${campaignId}`))
        .narration,
    ).toBe(false);
  });

  it("does not read another session's backlog after a same-route session swap", async () => {
    // KnowledgeSearch result links navigate to ?session=<id> on this same
    // route, so the page component stays mounted while `state` swaps to
    // another session's events. The backlog of the newly shown session must
    // stay silent just like the initial page load.
    vi.useFakeTimers();
    const synth = speechDevice();
    const otherSessionId = "22222222-2222-4222-8222-222222222222";
    const sessionTwo = pageData("dm", [
      event("s2-backlog", 1, "Session two story"),
    ]);
    const sessionOne = {
      ...pageData("dm", [event("s1-backlog", 1, "SESSION_ONE_CANARY")]),
      session: { id: otherSessionId, status: "active" },
    };
    let current = sessionTwo;
    stubFetch(() => current);
    await render(sessionTwo);
    await settle();
    expect(synth.speak).not.toHaveBeenCalled();
    current = sessionOne;
    await vi.advanceTimersByTimeAsync(2000);
    await settle();
    expect(feedIds()).toEqual(["event-s1-backlog"]);
    expect(synth.speak).not.toHaveBeenCalled();
  });

  it("reads new received reveals only after player opt-in and honors individual retractions", async () => {
    vi.useFakeTimers();
    const synth = speechDevice();
    let data = pageData("player", []);
    stubFetch(() => data);
    await render(data);
    checkbox("Read my reveals and private narration").click();
    await tick();
    data = pageData("player", [
      event("reveal", 1, "", {
        kind: "reveal",
        audience: "pcs",
        audience_pc_ids: [pcA],
        body: {
          retracted_entity_ids: ["gone"],
          reveals: [
            { entity_id: "known", name: "Mara", grant_scope: "name_only" },
            { entity_id: "gone", name: "RETRACTED_CANARY" },
            { entity_id: "silent", name: "SILENT_CANARY", silent: true },
          ],
        },
      }),
    ]);
    await vi.advanceTimersByTimeAsync(2000);
    await settle();
    expect(synth.speak).toHaveBeenCalledTimes(1);
    expect(synth.speak.mock.calls[0][0].text).toBe("Mara");
  });

  it("hides read-aloud controls when speech synthesis is unavailable", async () => {
    vi.stubGlobal("speechSynthesis", undefined);
    stubFetch(() => pageData("player", []));
    await render(pageData("player", []));
    expect(document.querySelector('[aria-label="Read aloud"]')).toBeNull();
  });

  it("saves and deletes DM voice presets through the state operations", async () => {
    const data = {
      ...pageData("dm", []),
      voices: [
        {
          speaker_key: "narrator",
          voice_hint: { lang: "en-US", names: ["English"] },
          rate: 1,
          pitch: 1,
        },
      ],
    };
    const fetch = stubFetch(() => data);
    await render(data);
    const set = async (label, value, type = "input") => {
      const input = document.querySelector(`[aria-label="${label}"]`);
      input.value = value;
      input.dispatchEvent(new Event(type, { bubbles: true }));
      await tick();
    };
    await set("Language hint", "en-GB");
    await set("Preferred voice names", "North, English");
    await set("Voice rate", "0.8");
    await set("Voice pitch", "0.6");
    document
      .querySelector(".voice-presets form")
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await settle();
    expect(posted(fetch)[0]).toMatchObject({
      operation: "saveVoice",
      speakerKey: "narrator",
      voice_hint: { lang: "en-GB", names: ["North", "English"] },
      rate: 0.8,
      pitch: 0.6,
    });
    [...document.querySelectorAll("button")]
      .find((button) => button.textContent === "Delete voice")
      .click();
    await settle();
    expect(posted(fetch)[1]).toMatchObject({
      operation: "deleteVoice",
      speakerKey: "narrator",
    });
  });
});

describe("voice preset editor across state polls", () => {
  it("keeps unsaved edits through a poll and reloads after the save lands", async () => {
    vi.useFakeTimers();
    const preset = (lang) => ({
      speaker_key: "narrator",
      voice_hint: { lang, names: ["English"] },
      rate: 1,
      pitch: 1,
    });
    // Every poll returns a fresh voices array, as the real BFF does.
    let current = { ...pageData("dm", []), voices: [preset("en-US")] };
    const fetch = stubFetch(() => ({
      ...current,
      voices: [...current.voices],
    }));
    await render(current);
    const field = () => document.querySelector('[aria-label="Language hint"]');
    field().value = "en-GB";
    field().dispatchEvent(new Event("input", { bubbles: true }));
    await tick();
    await vi.advanceTimersByTimeAsync(2000);
    await settle();
    expect(field().value).toBe("en-GB");
    document
      .querySelector(".voice-presets form")
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await settle();
    expect(posted(fetch)[0]).toMatchObject({
      operation: "saveVoice",
      voice_hint: { lang: "en-GB", names: ["English"] },
    });
    // The saved preset arrives on the next poll and the form reloads from it.
    current = { ...current, voices: [preset("fr-FR")] };
    await vi.advanceTimersByTimeAsync(2000);
    await settle();
    expect(field().value).toBe("fr-FR");
  });
});

describe("private session events from server projections", () => {
  it("renders a labelled knowledge search in the player view without exposing it as a DM control", async () => {
    stubFetch(() => pageData("player", []));
    await render(pageData("player", []));
    const input = document.querySelector('input[type="search"]');
    expect(input).not.toBeNull();
    expect(document.querySelector(`label[for="${input.id}"]`).textContent).toBe(
      "Search what your character knows",
    );
    expect(
      document.querySelector('[role="status"][aria-label="Session connection"]')
        .textContent,
    ).toBe("Live");
    expect(
      document.querySelector(
        '[role="status"][aria-label="Knowledge search status"]',
      ),
    ).not.toBeNull();
    await unmount(instance);
    instance = null;
    document.body.innerHTML = "";
    await render(pageData("dm", []));
    expect(
      document.querySelector(
        'section[aria-label="Character knowledge search"]',
      ),
    ).toBeNull();
  });

  it("renders a sequence anchor and keeps a historical search target during polling", async () => {
    const state = {
      ...pageData("player", [event("old-event", 42, "An earlier scene.")]),
      selectedSessionId: sessionId,
    };
    const fetch = stubFetch(() => state);
    vi.useFakeTimers();
    await render(state);
    expect(
      document.getElementById(`session-${sessionId}-event-42`),
    ).not.toBeNull();
    await vi.advanceTimersByTimeAsync(2000);
    await settle();
    expect(
      fetch.mock.calls.some(
        ([url]) => url === `${endpoint}?session=${sessionId}`,
      ),
    ).toBe(true);
  });

  const publicEvent = event("public-control", 1, "Everyone hears the bell.");
  const action = event("private-action-a", 2, "I conceal the silver compass.", {
    kind: "action",
    audience: "dm",
    author_member_id: "member-a",
  });
  const reply = event(
    "private-reply-a",
    3,
    "Only you feel the compass pulse.",
    {
      audience: "pcs",
      audience_pc_ids: [pcA],
      body: {
        text: "Only you feel the compass pulse.",
        reply_to: action.id,
        resolved: true,
      },
    },
  );
  const characters = [
    { id: pcA, campaign_id: campaignId, character_name: "Aria" },
    { id: pcB, campaign_id: campaignId, character_name: "Bram" },
  ];
  const viewers = [
    { id: "user-a", role: "player", pcs: [characters[0]] },
    { id: "user-dm", role: "dm", pcs: characters },
    { id: "user-b", role: "player", pcs: [characters[1]] },
  ];

  // These are mocked backend responses, already scoped by the server to
  // each viewer. No browser/test filtering implements authorization here:
  // vitest exercises the actual BFF load/GET and page state/render paths.
  // Backend isolation tests remain the evidence for authorization itself.
  function projectedServer(viewer, initial, polled) {
    let events = initial;
    const token = `test-token-${viewer.id}`;
    const fetch = vi.fn(async (url, options) => {
      expect(options.headers["x-grimoire-token"]).toBe(token);
      const path = new URL(url).pathname;
      if (path.endsWith("/lobby"))
        return json({
          user: { id: viewer.id },
          campaigns: [
            {
              id: campaignId,
              name: "Adventure",
              role: viewer.role,
              player_character_id:
                viewer.role === "dm" ? null : viewer.pcs[0].id,
            },
          ],
        });
      if (path.endsWith("/characters")) return json(viewer.pcs);
      if (path.endsWith("/voices")) return json([]);
      if (path.endsWith("/entities"))
        return json({ items: [], next_cursor: null });
      if (path.endsWith("/sheets")) return json({ versions: [] });
      if (path.endsWith("/sessions"))
        return json([{ id: sessionId, status: "active" }]);
      if (path.endsWith("/events")) return json(events);
      if (path.endsWith("/journal")) return json({});
      if (path.endsWith("/members"))
        return json([
          { id: "member-a", role: "player", player_character_id: pcA },
          { id: "member-b", role: "player", player_character_id: pcB },
        ]);
      throw new Error(`Unexpected backend request ${url}`);
    });
    return {
      context: {
        fetch,
        cookies: { get: () => token },
        params: { id: campaignId },
        setHeaders: vi.fn(),
      },
      poll: () => {
        events = polled;
      },
    };
  }

  async function renderViewers(projections, assertions) {
    vi.useFakeTimers();
    vi.stubEnv("API_BASE", "http://backend.test");
    vi.stubEnv("GRIMOIRE_PLAY_ENABLED", "true");
    const servers = viewers.map((viewer) => {
      const [initial, polled] = projections[viewer.id];
      return projectedServer(viewer, initial, polled);
    });
    // Interleave the real BFF loads to catch shared cross-request state.
    const initialStates = await Promise.all(
      servers.map((server) => load(server.context)),
    );
    for (const [index, viewer] of viewers.entries()) {
      const server = servers[index];
      expect(initialStates[index].user.id).toBe(viewer.id);
      expect(initialStates[index].characters.map((pc) => pc.id)).toEqual(
        viewer.pcs.map((pc) => pc.id),
      );
      const fetch = vi.fn(async (url) => {
        if (String(url).includes("?notes=")) return json([]);
        expect(url).toBe(endpoint);
        return GET({
          ...server.context,
          url: new URL(url, "http://frontend.test"),
        });
      });
      vi.stubGlobal("fetch", fetch);
      await render(initialStates[index]);
      assertions(viewer, false);
      server.poll();
      await vi.advanceTimersByTimeAsync(2000);
      await settle();
      expect(statePolls(fetch)).toHaveLength(1);
      assertions(viewer, true);
      await unmount(instance);
      instance = undefined;
      document.body.innerHTML = "";
    }
  }

  function assertAction(viewer, resolved = false) {
    expect(
      document.getElementById(`event-${publicEvent.id}`).textContent,
    ).toContain(publicEvent.body.text);
    if (viewer.id === "user-b") {
      expect(feedIds()).toEqual([`event-${publicEvent.id}`]);
      expect(document.body.innerHTML).not.toContain(action.id);
      expect(document.body.textContent).not.toContain(action.body.text);
      expect(document.querySelector(".private-action")).toBeNull();
    } else {
      const rendered = document.getElementById(`event-${action.id}`);
      expect(rendered.textContent).toContain(action.body.text);
      expect(rendered.querySelector(".private-action small").textContent).toBe(
        resolved ? "Resolved" : "Waiting for DM",
      );
      if (viewer.role === "dm")
        expect(rendered.textContent.includes("Reply privately to Aria")).toBe(
          !resolved,
        );
    }
  }

  it("renders A's pending private action for A and DM, never B, on load and poll", async () => {
    await renderViewers(
      {
        "user-a": [
          [publicEvent, action],
          [publicEvent, action],
        ],
        "user-dm": [
          [publicEvent, action],
          [publicEvent, action],
        ],
        "user-b": [[publicEvent], [publicEvent]],
      },
      (viewer) => assertAction(viewer),
    );
  });

  it("renders the resolved DM reply only for A and DM and clears their pending indicator", async () => {
    await renderViewers(
      {
        "user-a": [
          [publicEvent, action],
          [publicEvent, action, reply],
        ],
        "user-dm": [
          [publicEvent, action],
          [publicEvent, action, reply],
        ],
        "user-b": [[publicEvent], [publicEvent]],
      },
      (viewer, polled) => {
        assertAction(viewer, polled);
        if (viewer.id === "user-b" || !polled) {
          expect(document.body.innerHTML).not.toContain(reply.id);
          expect(document.body.textContent).not.toContain(reply.body.text);
        } else {
          expect(feedIds()).toEqual([
            `event-${publicEvent.id}`,
            `event-${action.id}`,
            `event-${reply.id}`,
          ]);
          expect(
            document.getElementById(`event-${reply.id}`).textContent,
          ).toContain(reply.body.text);
          expect(document.body.textContent).not.toContain("Waiting for DM");
        }
      },
    );
  });
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

describe("dice tray roll mode", () => {
  const modeSelect = () => document.querySelector('[aria-label="Roll mode"]');
  async function chooseMode(value) {
    const input = document.querySelector(
      `input[name="roll-mode"][value="${value}"]`,
    );
    input.checked = true;
    input.dispatchEvent(new Event("change", { bubbles: true }));
    await tick();
  }
  const quickButton = (label) =>
    [...document.querySelectorAll('div[aria-label="Quick rolls"] button')].find(
      (button) => button.textContent.trim() === label,
    );
  async function submitFormula(text) {
    const input = document.querySelector(
      'details.dice-tray input[placeholder="d20, 2d6+3, 1d20adv"]',
    );
    input.value = text;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await tick();
    document
      .querySelector("details.dice-tray form")
      .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await settle();
  }

  it("shows the mode control to the DM, and Advantage on quick d20 posts d20adv", async () => {
    const fetch = stubFetch(() => pageData("dm", []));
    await render(pageData("dm", []));
    expect(modeSelect()).not.toBeNull();
    // Sheet controls stay player-only and approved-sheet-only.
    expect(
      document.querySelector('select[aria-label="Sheet roll type"]'),
    ).toBeNull();
    expect(
      document.querySelector('div[aria-label="Approved sheet rolls"]'),
    ).toBeNull();

    await chooseMode("adv");
    quickButton("d20").click();
    await settle();
    expect(posted(fetch)).toEqual([
      expect.objectContaining({ operation: "roll", formula: "d20adv" }),
    ]);

    // Other dice ignore the mode.
    quickButton("d6").click();
    await settle();
    expect(posted(fetch)[1]).toMatchObject({
      operation: "roll",
      formula: "d6",
    });
  });

  it("shows the mode control to a player without an approved sheet", async () => {
    const fetch = stubFetch(() => pageData("player", []));
    await render(pageData("player", []));
    expect(modeSelect()).not.toBeNull();
    expect(
      document.querySelector('div[aria-label="Approved sheet rolls"]'),
    ).toBeNull();

    await chooseMode("dis");
    quickButton("d20").click();
    await settle();
    expect(posted(fetch)).toEqual([
      expect.objectContaining({ operation: "roll", formula: "d20dis" }),
    ]);
  });

  it("applies the mode to a bare d20 formula but leaves other formulas alone", async () => {
    const fetch = stubFetch(() => pageData("player", []));
    await render(pageData("player", []));
    await chooseMode("adv");

    await submitFormula("d20");
    expect(posted(fetch)[0]).toMatchObject({
      operation: "roll",
      formula: "d20adv",
    });
    await submitFormula("2d6+3");
    expect(posted(fetch)[1]).toMatchObject({
      operation: "roll",
      formula: "2d6+3",
    });
    await submitFormula("1d20dis");
    expect(posted(fetch)[2]).toMatchObject({
      operation: "roll",
      formula: "1d20dis",
    });
  });
});

// Canonical GET /campaigns/{id}/sessions/{sid}/journal bodies, as the BFF
// passes them through to the page.
const emptyJournal = () => ({
  learned: [],
  received: [],
  people_and_places: [],
  rolls: [],
  open_threads: [],
  truncated: false,
});
const rollEntry = (id, label, total) => ({
  id,
  seq: 1,
  kind: "roll",
  audience: "table",
  audience_pc_ids: [],
  author_member_id: null,
  body: { label, total, formula: "1d20" },
});
const learnedEntry = (overrides = {}) => ({
  event_id: "event-reveal",
  seq: 2,
  entity_id: "entity-mara",
  name: "Mara",
  entity_type: "npc",
  grant_scope: "partial",
  retracted: false,
  entity: { revealed_details: { clue: "Mara knows the cellar door." } },
  ...overrides,
});

const journalRegion = () =>
  document.querySelector('section[aria-label="Session journal"]');
const journalText = () => journalRegion()?.textContent ?? "";
const pressed = (label) =>
  [...document.querySelectorAll("button")]
    .find((button) => button.textContent.trim() === label)
    ?.getAttribute("aria-pressed");
async function click(label, scope = document) {
  const button = [...scope.querySelectorAll("button")].find(
    (node) => node.textContent.trim() === label,
  );
  expect(button, `button ${label}`).toBeTruthy();
  button.click();
  await settle();
}
const buttonLabels = (scope = document) =>
  [...scope.querySelectorAll("button")].map((node) => node.textContent.trim());

describe("session journal tab", () => {
  it("replaces the journal on every feed poll and keeps the chosen audience", async () => {
    vi.useFakeTimers();
    const filled = {
      ...emptyJournal(),
      learned: [learnedEntry()],
      rolls: [rollEntry("roll-1", "MINE_ONLY_CANARY search", 17)],
    };
    const changed = {
      ...filled,
      learned: [
        learnedEntry({
          entity: {
            revealed_details: { clue: "Mara hid a key under the cellar." },
          },
        }),
      ],
      open_threads: [
        {
          id: "act-1",
          seq: 5,
          kind: "action",
          audience: "dm",
          audience_pc_ids: [],
          author_member_id: "member-a",
          body: { text: "Is the cellar locked?" },
        },
      ],
    };
    const opening = rollEntry("party-1", "PARTY_ONLY_CANARY opening", 11);
    const ambush = rollEntry("party-2", "PARTY_ONLY_CANARY ambush", 6);
    const retreat = rollEntry("party-3", "PARTY_ONLY_CANARY retreat", 3);
    const partyAt = (rolls, truncated = false) => ({
      ...emptyJournal(),
      rolls,
      truncated,
    });
    // Index n is what the journal holds after n polls.
    const mine = [emptyJournal(), filled, changed, changed, changed];
    const party = [
      partyAt([opening]),
      partyAt([opening]),
      partyAt([opening]),
      partyAt([opening, ambush], true),
      partyAt([opening, ambush, retreat], true),
    ];
    const at = (step) => ({ mine: mine[step], party: party[step] });
    let step = 0;
    const fetch = stubFetch(() => {
      step += 1;
      return pageData("player", [], { journal: at(Math.min(step, 4)) });
    });
    // Mine is initially empty; the first poll fills it.
    await render(pageData("player", [], { journal: at(0) }));
    await click("Journal");
    expect(pressed("Mine")).toBe("true");
    expect(journalText()).toContain("No discoveries yet.");
    expect(journalText()).not.toContain("MINE_ONLY_CANARY");

    await vi.advanceTimersByTimeAsync(2000);
    await settle();
    expect(statePolls(fetch)).toHaveLength(1);
    expect(journalText()).toContain("Mara knows the cellar door.");
    expect(journalText()).toContain("MINE_ONLY_CANARY search");
    expect(journalText()).not.toContain("PARTY_ONLY_CANARY");
    expect(journalText()).not.toContain("No discoveries yet.");

    // A second consecutive poll changes the same entry in place.
    await vi.advanceTimersByTimeAsync(2000);
    await settle();
    expect(statePolls(fetch)).toHaveLength(2);
    expect(journalText()).toContain("Mara hid a key under the cellar.");
    expect(journalText()).not.toContain("Mara knows the cellar door.");
    expect(journalText()).toContain("Is the cellar locked?");

    // Choosing Party shows the party projection, never the member's own.
    await click("Party");
    expect(pressed("Party")).toBe("true");
    expect(pressed("Mine")).toBe("false");
    expect(journalText()).toContain("PARTY_ONLY_CANARY opening");
    expect(journalText()).not.toContain("MINE_ONLY_CANARY");
    expect(journalText()).not.toContain("Mara hid a key");

    // The next polls update Party in place without resetting the choice.
    await vi.advanceTimersByTimeAsync(2000);
    await settle();
    expect(statePolls(fetch)).toHaveLength(3);
    expect(pressed("Party")).toBe("true");
    expect(journalText()).toContain("PARTY_ONLY_CANARY ambush");
    expect(journalText()).not.toContain("PARTY_ONLY_CANARY retreat");
    expect(journalText()).toContain("This journal is incomplete");
    expect(journalText()).not.toContain("MINE_ONLY_CANARY");

    await vi.advanceTimersByTimeAsync(2000);
    await settle();
    expect(statePolls(fetch)).toHaveLength(4);
    expect(pressed("Party")).toBe("true");
    expect(journalText()).toContain("PARTY_ONLY_CANARY retreat");
    expect(journalText()).not.toContain("MINE_ONLY_CANARY");

    // Switching back shows the latest Mine data from the same polls.
    await click("Mine");
    expect(journalText()).toContain("Mara hid a key under the cellar.");
    expect(journalText()).not.toContain("PARTY_ONLY_CANARY");
  });

  it("shows a safe message with no session or no journal", async () => {
    vi.useFakeTimers();
    stubFetch(() => pageData("player", [], { session: null }));
    await render(pageData("player", [], { session: null }));
    await click("Journal");
    expect(journalRegion()).toBeNull();
    expect(document.body.textContent).toContain(
      "Your journal starts when the session does.",
    );
    await vi.advanceTimersByTimeAsync(2000);
    await settle();
    expect(journalRegion()).toBeNull();
    expect(document.querySelector('[role="status"]').textContent).toBe("Live");
  });

  it("recovers when the journal is unavailable and a later poll supplies it", async () => {
    vi.useFakeTimers();
    stubFetch(() =>
      pageData("player", [], {
        journal: {
          mine: { ...emptyJournal(), learned: [learnedEntry()] },
          party: emptyJournal(),
        },
      }),
    );
    await render(pageData("player", [], { journal: null }));
    await click("Journal");
    expect(journalRegion()).toBeNull();
    expect(document.body.textContent).toContain("The journal is unavailable");
    await vi.advanceTimersByTimeAsync(2000);
    await settle();
    expect(journalText()).toContain("Mara knows the cellar door.");
  });

  it("links entries to the story and the knowledge drawer, never for retracted or name-only", async () => {
    vi.useFakeTimers();
    const journal = {
      mine: {
        ...emptyJournal(),
        learned: [
          learnedEntry(),
          learnedEntry({
            event_id: "event-gone",
            entity_id: "entity-gone",
            name: "Retracted Rook",
            retracted: true,
            entity: undefined,
          }),
          learnedEntry({
            event_id: "event-name",
            entity_id: "entity-name",
            name: "Recognised Reeve",
            grant_scope: "name_only",
            entity: { recognition_only: true, name: "Recognised Reeve" },
          }),
        ],
        received: [
          {
            id: "event-handout",
            seq: 3,
            kind: "handout",
            audience: "table",
            body: { text: "A torn map" },
          },
        ],
      },
      party: emptyJournal(),
    };
    const feed = [event("event-reveal", 2, "Mara is revealed.")];
    const fetch = stubFetch(() => pageData("player", feed, { journal }));
    await render(pageData("player", feed, { journal }));
    await click("Journal");

    const learned = journalRegion().querySelector(
      'section[aria-labelledby$="-learned"]',
    );
    expect(buttonLabels(learned)).toEqual([
      "Show in story",
      "Explore Mara",
      "Show in story",
      "Show in story",
    ]);
    expect(journalText()).toContain("You recognize this name.");
    expect(buttonLabels(journalRegion())).not.toContain(
      "Explore Retracted Rook",
    );
    expect(buttonLabels(journalRegion())).not.toContain(
      "Explore Recognised Reeve",
    );

    await click("Explore Mara");
    expect(
      document.querySelector('section[aria-label="Knowledge detail"]'),
    ).toBeTruthy();
    expect(fetch.mock.calls.map(([url]) => url)).toContain(
      `${endpoint}?entity=entity-mara`,
    );
    await click("Close knowledge");
    expect(
      document.querySelector('section[aria-label="Knowledge detail"]'),
    ).toBe(null);

    const scrolled = vi.fn();
    document.getElementById("event-event-reveal").scrollIntoView = scrolled;
    await click("Show in story", learned);
    expect(document.querySelector(".layout").hidden).toBe(false);
    expect(scrolled).toHaveBeenCalledWith({ block: "center" });
  });
});

describe("handouts in the session feed", () => {
  const handoutEvent = event("handout-1", 1, "", {
    kind: "handout",
    body: {
      title: "Letter from the baron",
      markdown: "Meet me at **dusk**.",
      image: { source: "upload", key: "campaigns/x/handouts/y.png" },
    },
  });
  const composerForm = () =>
    document.querySelector('form[aria-label="Send a handout"]');

  it("gives a player the card with a proxied image and a pin that posts the event", async () => {
    const fetch = stubFetch(() => pageData("player", [handoutEvent]));
    await render(pageData("player", [handoutEvent]));
    const card = document.querySelector("#event-handout-1 .handout-card");
    expect(card.querySelector("h3").textContent).toBe("Letter from the baron");
    expect(card.querySelector("img").getAttribute("src")).toBe(
      `/grimoire/campaigns/${campaignId}/session/${sessionId}/events/handout-1/image`,
    );
    expect(composerForm()).toBeNull();
    const buttons = [
      ...document.querySelectorAll("#event-handout-1 button"),
    ].filter((button) => /Pin/.test(button.textContent));
    expect(buttons).toHaveLength(1);
    buttons[0].click();
    await settle();
    expect(posted(fetch)).toEqual([
      expect.objectContaining({
        operation: "note",
        fromEventId: "handout-1",
      }),
    ]);
  });

  it("gives the DM the composer and a card without a pin", async () => {
    stubFetch(() => pageData("dm", [handoutEvent]));
    await render(pageData("dm", [handoutEvent]));
    expect(composerForm()).not.toBeNull();
    expect(
      [...document.querySelectorAll("#event-handout-1 button")].filter(
        (button) => /Pin/.test(button.textContent),
      ),
    ).toHaveLength(0);
  });

  it("posts the composed handout from the DM composer", async () => {
    const fetch = stubFetch(() => pageData("dm", []));
    await render(pageData("dm", []));
    const title = composerForm().querySelector("input[required]");
    title.value = "Map";
    title.dispatchEvent(new Event("input", { bubbles: true }));
    await tick();
    composerForm().dispatchEvent(
      new Event("submit", { bubbles: true, cancelable: true }),
    );
    await settle();
    expect(posted(fetch)).toEqual([
      expect.objectContaining({
        operation: "handout",
        sessionId,
        title: "Map",
        audience: "table",
        pcIds: [],
      }),
    ]);
  });
});
