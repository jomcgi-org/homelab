// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Lobby from "./+page.svelte";
import Sheets from "./sheets/+page.svelte";
import PrivateSheets from "../../private/grimoire/+page.svelte";
import Session from "./campaigns/[id]/session/+page.svelte";
import Notes from "./campaigns/[id]/notes/+page.svelte";
import Journal from "./campaigns/[id]/journal/+page.svelte";
import Entity from "./campaigns/[id]/entities/[entityId]/+page.svelte";
import Grants from "./campaigns/[id]/grants/+page.svelte";

const campaign = { id: "demo-campaign", name: "Paper coast", role: "dm" };
const user = { id: "demo-dm", display_name: "Rowan", email: "dm@example.test" };
let instance;
const text = () => document.body.textContent;
const buttons = () => [...document.querySelectorAll("button")];
async function show(Page, data, form) {
  instance = mount(Page, { target: document.body, props: { data, form } });
  await tick();
  await tick();
}
function sheet() {
  expect(
    document.querySelectorAll("main.td.sheet.grimoire-sheet"),
  ).toHaveLength(1);
  expect(
    document.querySelector(".frame .journal .spine .trail-dock"),
  ).toBeTruthy();
  expect(document.querySelector('a[href="/grimoire"]')).toBeTruthy();
  expect(document.querySelector('a[href="/grimoire/sheets"]')).toBeTruthy();
  expect(document.querySelector("button.scheme")).toBeTruthy();
}
beforeEach(() => {
  delete document.documentElement.dataset.theme;
  localStorage.clear();
  vi.spyOn(window, "matchMedia").mockImplementation((media) => ({
    matches: media.includes("reduce"),
    media,
    addEventListener() {},
    removeEventListener() {},
  }));
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("[]")),
  );
});
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = undefined;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("friends Grimoire production page DOM", () => {
  for (const role of ["dm", "player"]) {
    it(`keeps ${role} campaign links, actions and authorization controls`, async () => {
      await show(Lobby, {
        user,
        invitations: [],
        campaigns: [
          {
            ...campaign,
            role,
            is_owner: role === "dm",
            join_links_error: true,
            members: [],
            invitations: [],
            unassigned_characters: [],
            player_character_id: "demo-pc",
          },
        ],
      });
      sheet();
      expect(
        document.querySelector(
          'a[href="/grimoire/campaigns/demo-campaign/session"]',
        ),
      ).toBeTruthy();
      expect(
        document.querySelector('a[href="/grimoire/sheets#character-demo-pc"]'),
      ).toBeTruthy();
      expect(document.querySelector('form[action="?/invite"]') !== null).toBe(
        role === "dm",
      );
      expect(document.querySelector('form[action="?/create"]')).toBeTruthy();
    });
    it(`renders ${role} session controls and empty/error state`, async () => {
      await show(Session, {
        campaign: { ...campaign, role },
        characters: [],
        session: null,
        events: [],
        journal: null,
        user,
      });
      sheet();
      expect(text()).toContain("The story begins here.");
      expect(
        buttons().some((button) => button.textContent === "Start session"),
      ).toBe(role === "dm");
      expect(
        document.querySelector('nav[aria-label="Session sections"]'),
      ).toBeTruthy();
      buttons()
        .find((button) => button.textContent === "Journal")
        .click();
      await tick();
      expect(text()).toContain("Your journal starts when the session does.");
    });
    it(`keeps ${role} notes audience, filters and create actions`, async () => {
      await show(
        Notes,
        {
          campaign: { ...campaign, role, player_character_id: "demo-pc" },
          notes: [],
          kind: role === "dm" ? "party" : "character",
          q: "paper coast",
        },
        { error: "Demo save failed" },
      );
      sheet();
      expect(document.querySelector('[role="alert"]').textContent).toBe(
        "Demo save failed",
      );
      expect(
        document.querySelector('[role="tablist"][aria-label="Note audience"]'),
      ).toBeTruthy();
      expect(
        document.querySelector(
          `form[action="?kind=${role === "dm" ? "party" : "character"}&q=paper+coast&/create"]`,
        ),
      ).toBeTruthy();
      for (const tab of document.querySelectorAll(
        '[role="tab"][aria-controls]',
      )) {
        expect(
          document.getElementById(tab.getAttribute("aria-controls")),
        ).toBeTruthy();
      }
    });
  }

  it("shows invitation actions, empty campaigns, server errors and the scheme switch", async () => {
    await show(
      Lobby,
      {
        user,
        campaigns: [],
        invitations: [{ id: "demo-invite", campaign_name: "Paper coast" }],
      },
      { error: "Demo invitation expired" },
    );
    sheet();
    expect(text()).toContain("No campaigns yet.");
    expect(document.querySelector('[role="alert"]').textContent).toBe(
      "Demo invitation expired",
    );
    expect(document.querySelector('button[formaction="?/decline"]').type).toBe(
      "submit",
    );
    expect(
      document.querySelector(
        'form[action="?/accept"] input[name="invitation_id"]',
      ).value,
    ).toBe("demo-invite");
    document.querySelector("button.scheme").click();
    await tick();
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("td-theme")).toBe("dark");
  });

  for (const Page of [Sheets, PrivateSheets]) {
    for (const role of ["dm", "player"]) {
      it(`preserves ${Page === Sheets ? "friends" : "private"} ${role} character actions and anchor`, async () => {
        await show(Page, {
          groups: [
            {
              campaign,
              workspaces: [
                {
                  character: { id: "demo-pc", character_name: "Elowen" },
                  viewer_role: role,
                  versions:
                    role === "dm"
                      ? [
                          {
                            id: "demo-version",
                            version: 1,
                            status: "submitted",
                            created_at: "2026-11-10T00:00:00Z",
                          },
                        ]
                      : [],
                },
              ],
            },
          ],
        });
        expect(document.getElementById("character-demo-pc")).toBeTruthy();
        expect(document.querySelector('form[action="?/decide"]') !== null).toBe(
          role === "dm",
        );
        expect(document.querySelector('form[action="?/save"]') !== null).toBe(
          role === "player",
        );
        expect(document.querySelector('input[name="character_id"]').value).toBe(
          "demo-pc",
        );
        if (Page === Sheets) sheet();
        else expect(document.querySelector("main.sheet")).toBeNull();
      });
    }
    for (const unavailable of [false, true]) {
      it(`preserves ${Page === Sheets ? "friends" : "private"} character empty/unavailable presentation (${unavailable})`, async () => {
        await show(
          Page,
          { groups: [], unavailable, message: "Demo sheets unavailable" },
          { error: "Demo edit failed" },
        );
        expect(text()).toContain(
          unavailable ? "Sheets are unavailable" : "No campaigns yet",
        );
        expect(document.querySelector('[role="alert"]').textContent).toBe(
          "Demo edit failed",
        );
        if (Page === Sheets) sheet();
        else expect(document.querySelector("main.sheet")).toBeNull();
      });
    }
  }

  it("renders the campaign journal audience and empty state", async () => {
    await show(Journal, {
      campaign,
      view: "party",
      journal: { sessions: [], next_cursor: "older" },
    });
    sheet();
    expect(text()).toContain("No sessions recorded yet.");
    expect(
      document
        .querySelector('button[value="party"]')
        .getAttribute("aria-pressed"),
    ).toBe("true");
    expect(
      document.querySelector('a[href="?view=party&cursor=older"]'),
    ).toBeTruthy();
  });

  it("renders only the entity projection supplied by the route", async () => {
    await show(Entity, {
      campaignId: campaign.id,
      entity: {
        id: "demo-entity",
        entity_type: "person",
        name: "Mara",
        occupation: "Cartographer",
      },
    });
    sheet();
    expect(text()).toContain("Mara");
    expect(text()).toContain("Cartographer");
    expect(
      document.querySelector(
        'a[href="/grimoire/campaigns/demo-campaign/session"]',
      ),
    ).toBeTruthy();
  });

  it("renders the DM grants matrix and filters without changing route contracts", async () => {
    await show(Grants, {
      campaign,
      entities: [{ id: "demo-entity", name: "Mara", entity_type: "person" }],
      characters: [{ id: "demo-pc", character_name: "Elowen" }],
      grants: [],
      sessions: [],
    });
    sheet();
    expect(
      document.querySelector('button[aria-label="Edit Mara for Elowen"]'),
    ).toBeTruthy();
    expect(text()).toContain("Not granted");
    const search = document.querySelector(".filters input");
    search.value = "missing";
    search.dispatchEvent(new Event("input", { bubbles: true }));
    await tick();
    expect(document.querySelector("tbody").textContent.trim()).toBe("");
  });
});
