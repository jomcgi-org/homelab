import { act, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { keys } from "../../api/lobby";
import type { Routes } from "../../test/backend";
import { chooseOption, findToast, openApp } from "../../test/app";
import {
  campaigns,
  characters,
  ids,
  lobby,
  members,
} from "../../test/fixtures";
import { event, sessionRoutes } from "../../test/fixtures/session";
import {
  brannochDerived,
  brannochSubmitted,
  dmHistory,
  mira,
  miraDraft,
  miraSheet,
  playerHistory,
  sheetsRoutes,
  sheetVersion,
} from "../../test/fixtures/sheets";

const SHEETS =
  "GET /campaigns/{campaign_id}/characters/{player_character_id}/sheets";
const UPDATE =
  "PATCH /campaigns/{campaign_id}/characters/{player_character_id}/sheets/{sheet_version_id}";

const playerSheets = (routes: Routes = {}): Routes => ({
  ...sheetsRoutes,
  "GET /lobby": lobby({ campaigns: [campaigns.joined] }),
  ...routes,
});

// Exercise actual polling without focus or navigation triggering a refetch.
const refreshed = (assertion: () => void) =>
  waitFor(assertion, { timeout: 6_000 });

describe("Changes from another seat", { timeout: 15_000 }, () => {
  it("refreshes ordinary lobby changes at the existing fifteen-second interval", async () => {
    const { backend } = openApp({
      "GET /lobby": lobby({ campaigns: [campaigns.seatless] }),
    });
    const campaign = await screen.findByRole("article", {
      name: campaigns.seatless.name,
    });
    expect(
      within(campaign).getByRole("button", { name: "Create your character" }),
    ).toBeTruthy();

    backend.set({
      "GET /lobby": lobby({
        campaigns: [
          {
            ...campaigns.seatless,
            name: "The renamed campaign",
          },
        ],
      }),
    });

    await waitFor(
      () =>
        expect(
          screen.getByRole("article", { name: "The renamed campaign" }),
        ).toBeTruthy(),
      { timeout: 16_000 },
    );
    expect(
      within(campaign).getByRole("button", { name: "Create your character" }),
    ).toBeTruthy();
  }, 20_000);

  it("shows a new submission on the DM's open sheet", async () => {
    const { backend } = openApp(
      {
        ...sheetsRoutes,
        "GET /lobby": lobby({ campaigns: [campaigns.owned] }),
        [SHEETS]: dmHistory([]),
      },
      "/sheets",
    );
    await screen.findByText("No versions submitted.");
    backend.set({ [SHEETS]: dmHistory([brannochSubmitted]) });
    await refreshed(() =>
      expect(
        screen.getByRole("button", { name: "Approve version" }),
      ).toBeTruthy(),
    );
  });

  it("lets the player edit when the DM returns their submitted version", async () => {
    const submitted = sheetVersion({ ...miraDraft, status: "submitted" });
    const { backend } = openApp(
      playerSheets({ [SHEETS]: playerHistory([submitted]) }),
      "/sheets",
    );
    await screen.findByText("Submitted");
    expect(screen.queryByRole("spinbutton", { name: "Level" })).toBeNull();
    backend.set({
      [SHEETS]: playerHistory([
        {
          ...submitted,
          status: "returned",
          decision_comment: "Use level three.",
        },
      ]),
    });
    await refreshed(() =>
      expect(screen.getByRole("spinbutton", { name: "Level" })).toBeTruthy(),
    );
    expect(screen.getByText("Use level three.")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Start new draft" }),
    ).toBeTruthy();
  });

  it("updates the DM roster after a player joins with the existing character choices", async () => {
    const { backend, user } = openApp({
      "GET /lobby": lobby({ campaigns: [campaigns.owned] }),
      "GET /campaigns/{campaign_id}/members": [members[0]!],
      "GET /campaigns/{campaign_id}/characters": characters,
      "GET /campaigns/{campaign_id}/invitations": [],
      "GET /campaigns/{campaign_id}/join-links": { status: 503 },
    });
    const campaign = await screen.findByRole("article", {
      name: campaigns.owned.name,
    });
    await user.click(within(campaign).getByText("Players and invitations"));
    await within(campaign).findByText(members[0]!.email);
    backend.set({
      "GET /campaigns/{campaign_id}/members": members,
    });
    await refreshed(() =>
      expect(within(campaign).getByText(members[1]!.email)).toBeTruthy(),
    );
    await user.click(within(campaign).getByRole("button", { name: "Manage" }));
    const dialog = await screen.findByRole("dialog");
    await chooseOption(dialog, "Unassigned character", "Brannoch Stone");
    expect(
      screen.getByRole("button", { name: "Assign" }).hasAttribute("disabled"),
    ).toBe(false);
    await user.click(
      within(dialog).getAllByRole("button", { name: "Close" }).at(-1)!,
    );
  });

  it("adds a newly assigned character when the sheets character query is refreshed", async () => {
    const { backend, client } = openApp(
      playerSheets({ "GET /campaigns/{campaign_id}/characters": [] }),
      "/sheets",
    );
    await screen.findByText("No assigned characters.");
    backend.set({ "GET /campaigns/{campaign_id}/characters": [mira] });
    await act(async () => {
      await client.invalidateQueries({ queryKey: keys.characters(ids.joined) });
    });
    await screen.findByRole("region", { name: "Mira Vale" });
  });

  it("polls sheet history without accelerating the existing character access checks", async () => {
    const { backend, client, user } = openApp(
      playerSheets({ [SHEETS]: playerHistory([miraDraft]) }),
      "/sheets",
    );
    const level = await screen.findByRole<HTMLInputElement>("spinbutton", {
      name: "Level",
    });
    await user.clear(level);
    await user.type(level, "4");
    const characterReads = backend.callsTo(
      "GET /campaigns/{campaign_id}/characters",
    ).length;
    const lobbyReads = backend.callsTo("GET /lobby").length;
    const sheetReads = backend.callsTo(SHEETS).length;
    backend.set({
      "GET /campaigns/{campaign_id}/characters": [],
      "GET /lobby": lobby({
        campaigns: [{ ...campaigns.joined, player_character_id: null }],
      }),
      [SHEETS]: { status: 404 },
    });

    await refreshed(() =>
      expect(backend.callsTo(SHEETS).length).toBeGreaterThan(sheetReads),
    );
    expect(
      backend.callsTo("GET /campaigns/{campaign_id}/characters"),
    ).toHaveLength(characterReads);
    expect(backend.callsTo("GET /lobby")).toHaveLength(lobbyReads);
    expect(screen.getByRole("spinbutton", { name: "Level" })).toBe(level);
    expect(level.value).toBe("4");

    // Keep main's access check: a refreshed character list still removes the
    // old editor. This change introduces no recovery exception after access loss.
    await act(async () => {
      await client.invalidateQueries({
        queryKey: keys.characters(ids.joined),
        exact: true,
      });
    });
    await screen.findByText("No assigned characters.");
    expect(screen.queryByRole("region", { name: "Mira Vale" })).toBeNull();
    expect(screen.queryByRole("spinbutton", { name: "Level" })).toBeNull();
    expect(
      screen.queryByRole("region", { name: "Unsaved sheet changes" }),
    ).toBeNull();
  });

  it("updates an open Manage dialog when the player creates a character", async () => {
    const { backend, user } = openApp({
      "GET /lobby": lobby({ campaigns: [campaigns.owned] }),
      "GET /campaigns/{campaign_id}/members": members,
      "GET /campaigns/{campaign_id}/characters": characters,
      "GET /campaigns/{campaign_id}/invitations": [],
      "GET /campaigns/{campaign_id}/join-links": { status: 503 },
    });
    const campaign = await screen.findByRole("article", {
      name: campaigns.owned.name,
    });
    await user.click(within(campaign).getByText("Players and invitations"));
    await user.click(
      await within(campaign).findByRole("button", { name: "Manage" }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("No character assigned")).toBeTruthy();
    backend.set({
      "GET /campaigns/{campaign_id}/members": [
        members[0]!,
        {
          ...members[1]!,
          player_character_id: ids.spare,
          character_name: "Brannoch Stone",
        },
      ],
    });
    await refreshed(() =>
      expect(
        within(dialog).getByRole("button", { name: "Clear assignment" }),
      ).toBeTruthy(),
    );
    expect(within(dialog).getByText("Brannoch Stone")).toBeTruthy();
  });

  it("refreshes approved roll bonuses and ignores a newer unapproved version", async () => {
    const approved = {
      ...brannochSubmitted,
      version: 1,
      status: "approved" as const,
    };
    const next = {
      ...brannochSubmitted,
      derived: {
        ...brannochDerived,
        max_hit_points: 44,
        ability_modifiers: {
          ...brannochDerived.ability_modifiers,
          strength: 5,
        },
      },
    };
    const { backend, user } = openApp(
      {
        ...sessionRoutes,
        "GET /lobby": lobby({
          campaigns: [
            {
              ...campaigns.owned,
              role: "player",
              is_owner: false,
              player_character_id: ids.spare,
            },
          ],
        }),
        "GET /campaigns/{campaign_id}/characters": characters,
        [SHEETS]: dmHistory([approved]),
        "POST /campaigns/{campaign_id}/sessions/{session_id}/rolls": event({
          kind: "roll",
          body: { total: 15 },
        }),
      },
      `/campaigns/${ids.owned}/session`,
    );
    const character = await screen.findByRole(
      "complementary",
      {
        name: "Your character",
      },
      { timeout: 5_000 },
    );
    await within(character).findByText(/HP 36/);
    await user.click(screen.getByRole("button", { name: "Roll dice" }));
    backend.set({ [SHEETS]: dmHistory([next, approved]) });
    await refreshed(() =>
      expect(backend.callsTo(SHEETS).length).toBeGreaterThan(1),
    );
    expect(within(character).getByText(/HP 36/)).toBeTruthy();
    backend.set({
      [SHEETS]: dmHistory([{ ...next, status: "approved" }, approved]),
    });
    await refreshed(() =>
      expect(within(character).getByText(/HP 44/)).toBeTruthy(),
    );
    await user.click(screen.getByRole("button", { name: "Strength check" }));
    expect(
      backend.callsTo(
        "POST /campaigns/{campaign_id}/sessions/{session_id}/rolls",
      )[0]?.body,
    ).toMatchObject({ formula: "1d20+5", visibility: "table" });
  });

  it("keeps unsaved scores while a draft is saved in another tab", async () => {
    const { backend, user } = openApp(
      playerSheets({ [SHEETS]: playerHistory([miraDraft]) }),
      "/sheets",
    );
    const level = await screen.findByRole("spinbutton", { name: "Level" });
    await user.clear(level);
    await user.type(level, "4");
    backend.set({
      [SHEETS]: playerHistory([
        {
          ...miraDraft,
          sheet: { ...miraSheet, level: 5 },
          updated_at: "2026-11-05T18:00:00Z",
        },
      ]),
      [UPDATE]: () => {
        const saved = {
          ...miraDraft,
          sheet: { ...miraSheet, level: 4 },
          updated_at: "2026-11-05T19:00:00Z",
        };
        backend.set({ [SHEETS]: playerHistory([saved]) });
        return saved;
      },
    });
    await refreshed(() =>
      expect(backend.callsTo(SHEETS).length).toBeGreaterThan(1),
    );
    expect(
      (screen.getByRole("spinbutton", { name: "Level" }) as HTMLInputElement)
        .value,
    ).toBe("4");
    expect(document.activeElement).toBe(level);
    expect(
      screen
        .getByRole("button", { name: "Save draft" })
        .hasAttribute("disabled"),
    ).toBe(true);
    await user.click(
      screen.getByRole("button", { name: "Keep my conflicting edits" }),
    );
    await user.click(screen.getByRole("button", { name: "Save draft" }));
    await findToast("Draft saved.");
    expect(backend.callsTo(UPDATE)[0]?.body).toMatchObject({ level: 4 });
    expect(
      screen
        .getByRole("button", { name: "Submit to DM" })
        .hasAttribute("disabled"),
    ).toBe(false);
  });

  it("accepts a remote draft save when the local form has no unsaved edits", async () => {
    const { backend } = openApp(
      playerSheets({ [SHEETS]: playerHistory([miraDraft]) }),
      "/sheets",
    );
    await screen.findByRole("spinbutton", { name: "Level" });
    backend.set({
      [SHEETS]: playerHistory([
        {
          ...miraDraft,
          sheet: { ...miraSheet, level: 5 },
          updated_at: "2026-11-05T18:00:00Z",
        },
      ]),
    });
    await refreshed(() =>
      expect(
        (screen.getByRole("spinbutton", { name: "Level" }) as HTMLInputElement)
          .value,
      ).toBe("5"),
    );
    expect(
      screen
        .getByRole("button", { name: "Submit to DM" })
        .hasAttribute("disabled"),
    ).toBe(false);
  });
});
