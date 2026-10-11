// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Page from "./+page.svelte";
import fixture from "$lib/grimoire/fixtures/reveal-projections.json";
import dmRoutes from "$lib/grimoire/fixtures/dm-only-routes.json";
import { GET, POST } from "./state/+server.js";
import { plain, renderedFields } from "$lib/grimoire/test-helpers.js";

const campaignId = "11111111-1111-4111-8111-111111111111";
const sessionId = "33333333-3333-4333-8333-333333333333";
const endpoint = `/grimoire/campaigns/${campaignId}/session/state`;

// Backend routes a DM alone may reach, pinned to router.py's _require_dm
// guards by grimoire/visibility_test.py. Every player request is replayed
// through the real BFF below, so the check follows what the backend would see.
const pathPattern = (template) =>
  new RegExp(`^${template.replace(/\{[^}]+\}/g, "[^/]+")}$`);
const DM_ROUTES = dmRoutes.routes.map(([method, path]) => ({
  method,
  pattern: pathPattern(path),
}));

async function backendCalls([url, options]) {
  const calls = [];
  const backend = vi.fn(async (target, init = {}) => {
    calls.push({
      target: new URL(String(target)),
      method: init.method || "GET",
    });
    return json([]);
  });
  const event = {
    fetch: backend,
    cookies: { get: () => "signed-grimoire-token" },
    params: { id: campaignId },
  };
  if (options?.method === "POST")
    await POST({
      ...event,
      request: new Request("https://friends.jomcgi.dev/state", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: options.body,
      }),
    });
  else
    await GET({
      ...event,
      url: new URL(String(url), "https://friends.jomcgi.dev"),
    });
  return calls;
}

function revealEvent(item, seq) {
  const body = {
    entity_id: item.expected.id,
    name: item.expected.name,
    entity_type: item.expected.entity_type,
    grant_scope: item.name === "global_no_grant" ? "full" : item.name,
  };
  if (item.name !== "name_only") body.entity = item.expected;
  return {
    id: `reveal-${item.name}`,
    seq,
    kind: "reveal",
    audience: "pcs",
    audience_pc_ids: [fixture.viewer],
    author_member_id: "member-dm",
    body,
    created_at: "2026-10-10T12:00:00Z",
    retracted_at: null,
  };
}

function pageData(role, events) {
  return {
    campaign: { id: campaignId, name: "Adventure", role },
    characters: [
      { id: fixture.viewer, character_name: "Aria", approved: null },
    ],
    session: { id: sessionId, status: "active" },
    events,
    journal: null,
    user: { id: "viewer" },
  };
}

const json = (value) =>
  new Response(JSON.stringify(value), {
    status: 200,
    headers: { "content-type": "application/json" },
  });

let instance;
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  document.body.innerHTML = "";
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

async function settle(times = 8) {
  for (let step = 0; step < times; step += 1) await tick();
}

describe("player reveal cards", () => {
  it("show exactly the fixture's visible fields for each scope", async () => {
    const events = fixture.cases.map((item, index) =>
      revealEvent(item, index + 1),
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(pageData("player", events))),
    );
    instance = mount(Page, {
      target: document.body,
      props: { data: pageData("player", events) },
    });
    await settle();

    for (const item of fixture.cases) {
      const card = document.querySelector(
        `#event-reveal-${item.name} .knowledge-entry`,
      );
      expect(card, item.name).not.toBeNull();
      expect(renderedFields(card).sort()).toEqual(
        [...item.visible_fields].sort(),
      );
      const shown = item.expected.revealed_details || item.expected;
      for (const key of item.visible_fields)
        expect(
          card.querySelector(`[data-field="${key}"]`).textContent,
        ).toContain(plain(shown[key]));
      expect(card.textContent).toContain(item.expected.name);
      for (const hidden of item.hidden_values || [])
        expect(card.textContent).not.toContain(hidden);
      if (item.name === "name_only")
        expect(card.textContent).toContain("You recognize this name.");
      // Bookkeeping columns never read as knowledge.
      expect(card.textContent).not.toContain("homebrew");
      expect(card.textContent).not.toContain("2026-03-04");
    }
  });
});

describe("player view", () => {
  beforeEach(() => {
    process.env.API_BASE = "http://backend.test";
    process.env.GRIMOIRE_PLAY_ENABLED = "true";
  });

  it("never requests a DM-only route", async () => {
    vi.useFakeTimers();
    const events = [
      revealEvent(fixture.cases[0], 1),
      revealEvent(fixture.cases[1], 2),
    ];
    const fetch = vi.fn(async (url) => {
      if (String(url).includes("inventory=items"))
        return json([
          {
            id: campaignId,
            owner: "party",
            is_mine: false,
            name: "Potion",
            quantity: 3,
            notes: "",
            entity: null,
            hidden_from_party: false,
          },
          {
            id: sessionId,
            owner: fixture.viewer,
            is_mine: true,
            name: "Rope",
            quantity: 2,
            notes: "",
            entity: null,
            hidden_from_party: false,
          },
        ]);
      if (String(url).includes("entity="))
        return json({ ...fixture.cases[0].expected });
      return json(pageData("player", events));
    });
    vi.stubGlobal("fetch", fetch);
    instance = mount(Page, {
      target: document.body,
      props: { data: pageData("player", events) },
    });
    await settle();
    // Polling, then the reveal card's explore button.
    await vi.advanceTimersByTimeAsync(2000);
    document.dispatchEvent(new Event("visibilitychange"));
    await settle();
    [...document.querySelectorAll(".knowledge-entry button")]
      .find((button) => button.textContent.includes("Explore"))
      .click();
    await settle();

    [...document.querySelectorAll(".table-tabs button")]
      .find((node) => node.textContent === "Inventory")
      .click();
    await settle();
    for (const name of [
      "Move Rope to party pool",
      "Take Potion",
      "Save Rope",
    ]) {
      const button = [...document.querySelectorAll(".inventory button")].find(
        (node) => node.textContent === name,
      );
      expect(button, name).toBeTruthy();
      button
        .closest("form")
        .dispatchEvent(
          new Event("submit", { bubbles: true, cancelable: true }),
        );
      await settle();
    }
    const urls = fetch.mock.calls.map(([url]) => String(url));
    // The run is not vacuous: it polled state and opened the knowledge drawer.
    expect(urls).toContain(endpoint);
    expect(urls.some((url) => url.includes("?entity="))).toBe(true);
    // Fail closed: a player sends only the player operations.
    const operations = fetch.mock.calls
      .filter(([, options]) => options?.body)
      .map(([, options]) => JSON.parse(options.body).operation);
    expect(operations).toEqual(["moveItem", "moveItem", "updateItem"]);
    for (const operation of operations)
      expect(dmRoutes.player_operations).toContain(operation);
    // And nothing a player sends reaches a DM route behind the BFF.
    let replayed = 0;
    for (const request of fetch.mock.calls)
      for (const { target, method } of await backendCalls(request)) {
        replayed += 1;
        for (const route of DM_ROUTES)
          expect(
            route.method === method && route.pattern.test(target.pathname),
            `${method} ${target.pathname}`,
          ).toBe(false);
        for (const param of dmRoutes.dm_only_query_params)
          expect(target.searchParams.has(param), param).toBe(false);
      }
    expect(replayed).toBeGreaterThan(0);
    expect(
      [...document.querySelectorAll("button")].map((button) =>
        button.textContent.trim(),
      ),
    ).not.toContain("Reveal knowledge");
  });

  it("shows a player the turn strip but no initiative controls", async () => {
    const data = {
      ...pageData("player", []),
      initiative: {
        round: 2,
        active_index: 0,
        entries: [
          {
            label: "Aria",
            player_character_id: fixture.viewer,
            initiative: 15,
            hidden: false,
          },
        ],
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(data)),
    );
    instance = mount(Page, { target: document.body, props: { data } });
    await settle();
    expect(document.body.textContent).toContain("Round 2");
    const labels = [...document.querySelectorAll("button")].map((button) =>
      button.textContent.trim(),
    );
    for (const dmOnly of ["Save order", "Next turn", "End encounter"])
      expect(labels).not.toContain(dmOnly);
  });
});

// Every DM initiative operation must land on a route in the DM-only list, so
// the player-operation allowlist above is what keeps a player away from them.
describe("initiative operations", () => {
  beforeEach(() => {
    process.env.API_BASE = "http://backend.test";
    process.env.GRIMOIRE_PLAY_ENABLED = "true";
  });

  for (const [operation, extra] of Object.entries(
    dmRoutes.dm_initiative_operations,
  ))
    it(`${operation} reaches a DM-only route and is not a player operation`, async () => {
      expect(dmRoutes.player_operations).not.toContain(operation);
      const calls = await backendCalls([
        endpoint,
        {
          method: "POST",
          body: JSON.stringify({ sessionId, operation, ...extra }),
        },
      ]);
      expect(calls).toHaveLength(1);
      const [{ target, method }] = calls;
      expect(
        DM_ROUTES.some(
          (route) =>
            route.method === method && route.pattern.test(target.pathname),
        ),
        `${method} ${target.pathname}`,
      ).toBe(true);
    });
});
