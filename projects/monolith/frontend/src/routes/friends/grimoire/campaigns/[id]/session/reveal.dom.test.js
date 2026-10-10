// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Page from "./+page.svelte";
import fixture from "$lib/grimoire/fixtures/reveal-projections.json";
import { plain, renderedFields } from "$lib/grimoire/test-helpers.js";

const campaignId = "11111111-1111-4111-8111-111111111111";
const sessionId = "33333333-3333-4333-8333-333333333333";
const endpoint = `/grimoire/campaigns/${campaignId}/session/state`;

// Routes only a DM may reach, from the backend's _require_dm guards in
// grimoire/router.py (see ROUTES in grimoire/route_inventory_test.py):
// GET and POST /grants, POST /grants/bulk, POST /grants/preview,
// PATCH and DELETE /grants/{id}, and the DM-only not_granted_to entity
// filter. The BFF reaches them through the search query (`q`, `notGrantedTo`)
// and the operations below.
const DM_ONLY_URL = [/\/grants(\/|\?|$)/, /[?&](q|notGrantedTo|not_granted_to)=/];
const DM_ONLY_OPERATIONS = ["reveal", "previewReveal", "updateGrant", "revoke"];

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
    characters: [{ id: fixture.viewer, character_name: "Aria", approved: null }],
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
    const events = fixture.cases.map((item, index) => revealEvent(item, index + 1));
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
      const card = document.querySelector(`#event-reveal-${item.name} .knowledge-entry`);
      expect(card, item.name).not.toBeNull();
      expect(renderedFields(card).sort()).toEqual([...item.visible_fields].sort());
      const shown = item.expected.revealed_details || item.expected;
      for (const key of item.visible_fields)
        expect(card.querySelector(`[data-field="${key}"]`).textContent).toContain(
          plain(shown[key]),
        );
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
  it("never requests a DM-only route", async () => {
    vi.useFakeTimers();
    const events = [revealEvent(fixture.cases[0], 1), revealEvent(fixture.cases[1], 2)];
    const fetch = vi.fn(async (url) => {
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

    const urls = fetch.mock.calls.map(([url]) => String(url));
    // The run is not vacuous: it polled state and opened the knowledge drawer.
    expect(urls).toContain(endpoint);
    expect(urls.some((url) => url.includes("?entity="))).toBe(true);
    for (const url of urls)
      for (const pattern of DM_ONLY_URL) expect(url).not.toMatch(pattern);
    for (const [, options] of fetch.mock.calls) {
      if (!options?.body) continue;
      expect(DM_ONLY_OPERATIONS).not.toContain(JSON.parse(options.body).operation);
    }
    expect(
      [...document.querySelectorAll("button")].map((button) =>
        button.textContent.trim(),
      ),
    ).not.toContain("Reveal knowledge");
  });
});
