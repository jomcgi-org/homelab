import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Routes } from "../../test/backend";
import { chooseOption, findToast, openApp } from "../../test/app";
import { characters, ids, lobby, members } from "../../test/fixtures";

const dmRoutes: Routes = {
  "GET /lobby": lobby(),
  "GET /campaigns/{campaign_id}/members": members,
  "GET /campaigns/{campaign_id}/characters": characters,
  "GET /campaigns/{campaign_id}/invitations": [],
  "GET /campaigns/{campaign_id}/join-links": { status: 503 },
};

describe("Lobby", () => {
  it("lists invitations and campaigns with the viewer role", async () => {
    openApp(dmRoutes);
    expect(
      await screen.findByRole("heading", { name: "The Sunless Citadel" }),
    ).toBeTruthy();
    const owned = screen.getByRole("article", { name: "The Sunless Citadel" });
    expect(within(owned).getByText("Owner · DM")).toBeTruthy();
    const joined = screen.getByRole("article", { name: "Salt Marsh Nights" });
    expect(within(joined).getByText("Your character: Mira Vale")).toBeTruthy();
    expect(screen.getByText("Embers of the Old Court")).toBeTruthy();
  });

  it("links each campaign to its session", async () => {
    openApp(dmRoutes);
    const owned = await screen.findByRole("article", {
      name: "The Sunless Citadel",
    });
    const link = within(owned).getByRole("link", { name: "Open session" });
    expect(link.getAttribute("href")).toBe(`/campaigns/${ids.owned}/session`);
  });

  it("accepts an invitation and refreshes the lobby", async () => {
    const { user, backend } = openApp({
      ...dmRoutes,
      "POST /invitations/{invitation_id}/{decision}": (call) => ({
        ...lobby().invitations[0]!,
        status: call.params.decision === "accept" ? "accepted" : "declined",
      }),
    });
    await user.click(await screen.findByRole("button", { name: "Accept" }));
    const [call] = backend.callsTo(
      "POST /invitations/{invitation_id}/{decision}",
    );
    expect(call?.params).toEqual({
      invitation_id: ids.invitation,
      decision: "accept",
    });
    expect(await findToast("Invitation accepted.")).toBeTruthy();
    expect(backend.callsTo("GET /lobby").length).toBeGreaterThan(1);
  });

  it("creates a campaign from the trimmed name", async () => {
    const { user, backend } = openApp({
      ...dmRoutes,
      "POST /campaigns": (call) => ({
        id: ids.seatless,
        name: (call.body as { name: string }).name,
        dm_name: null,
        created_at: "2026-11-11T00:00:00Z",
      }),
    });
    const create = await screen.findByRole("button", {
      name: "Create campaign",
    });
    await user.type(screen.getByLabelText("Campaign name"), "  Ember Road ");
    await user.click(create);
    expect(backend.callsTo("POST /campaigns")[0]?.body).toEqual({
      name: "Ember Road",
    });
  });

  it("shows the refusal the backend gave", async () => {
    const { user } = openApp({
      ...dmRoutes,
      "POST /campaigns": {
        status: 403,
        detail: "You cannot create campaigns.",
      },
    });
    await user.type(
      await screen.findByLabelText("Campaign name"),
      "Ember Road",
    );
    await user.click(screen.getByRole("button", { name: "Create campaign" }));
    expect(await findToast("You cannot create campaigns.")).toBeTruthy();
  });

  it("lets a seatless player name a character", async () => {
    const { user, backend } = openApp({
      ...dmRoutes,
      "POST /campaigns/{campaign_id}/characters/self": (call) => ({
        ...characters[0]!,
        campaign_id: call.params.campaign_id!,
        character_name: (call.body as { name: string }).name,
      }),
    });
    const seatless = await screen.findByRole("article", {
      name: "The Long Road",
    });
    await user.type(within(seatless).getByLabelText("Character name"), "Ilsa");
    await user.click(
      within(seatless).getByRole("button", { name: "Create your character" }),
    );
    const [call] = backend.callsTo(
      "POST /campaigns/{campaign_id}/characters/self",
    );
    expect(call?.params.campaign_id).toBe(ids.seatless);
    expect(call?.body).toEqual({ name: "Ilsa" });
  });

  it("offers a retry when the lobby cannot load", async () => {
    openApp({ "GET /lobby": { status: 500, detail: "Database unavailable." } });
    expect(
      await screen.findByText("Could not load your campaigns"),
    ).toBeTruthy();
    expect(screen.getByText("Database unavailable.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
  });
});

describe("Lobby, as the DM", () => {
  async function roster(routes: Routes = {}) {
    const { user, backend } = openApp({ ...dmRoutes, ...routes });
    const owned = await screen.findByRole("article", {
      name: "The Sunless Citadel",
    });
    await user.click(within(owned).getByText("Players and invitations"));
    await within(owned).findByText("rowan@example.test");
    return { user, owned, backend };
  }

  it("assigns an unassigned character to a player", async () => {
    const { user, owned, backend } = await roster({
      "PUT /campaigns/{campaign_id}/members/{member_id}/character": {
        ...members[1]!,
        player_character_id: ids.spare,
        character_name: "Brannoch Stone",
      },
    });
    await user.click(within(owned).getByRole("button", { name: "Manage" }));
    const dialog = await screen.findByRole("dialog");
    await chooseOption(dialog, "Unassigned character", "Brannoch Stone");
    await user.click(within(dialog).getByRole("button", { name: "Assign" }));
    const [call] = backend.callsTo(
      "PUT /campaigns/{campaign_id}/members/{member_id}/character",
    );
    expect(call?.params.member_id).toBe(ids.playerMember);
    expect(call?.body).toEqual({ player_character_id: ids.spare });
  });

  it("confirms before removing a player", async () => {
    const { user, owned, backend } = await roster({
      "DELETE /campaigns/{campaign_id}/members/{member_id}": undefined,
    });
    await user.click(within(owned).getByRole("button", { name: "Manage" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Remove player",
      }),
    );
    expect(
      backend.callsTo("DELETE /campaigns/{campaign_id}/members/{member_id}"),
    ).toHaveLength(0);
    await user.click(
      within(await screen.findByRole("alertdialog")).getByRole("button", {
        name: "Remove player",
      }),
    );
    expect(
      backend.callsTo("DELETE /campaigns/{campaign_id}/members/{member_id}")[0]
        ?.params.member_id,
    ).toBe(ids.playerMember);
  });

  it("falls back to email invitations when links are unavailable", async () => {
    const { user, owned, backend } = await roster({
      "POST /campaigns/{campaign_id}/invitations": {
        ...lobby().invitations[0]!,
        invitee_email: "wren@example.test",
      },
    });
    await user.type(
      within(owned).getByLabelText("Registered player's email or @username"),
      "wren@example.test",
    );
    await user.click(
      within(owned).getByRole("button", { name: "Invite player" }),
    );
    expect(
      backend.callsTo("POST /campaigns/{campaign_id}/invitations")[0]?.body,
    ).toEqual({ email: "wren@example.test" });
  });
});
