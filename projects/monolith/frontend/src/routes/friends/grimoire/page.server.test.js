import { beforeEach, describe, expect, it, vi } from "vitest";
import { render } from "svelte/server";
import Lobby from "./+page.svelte";
import { actions, load } from "./+page.server.js";

const campaignId = "11111111-1111-4111-8111-111111111111";
const invitationId = "22222222-2222-4222-8222-222222222222";
const memberId = "33333333-3333-4333-8333-333333333333";
const characterId = "44444444-4444-4444-8444-444444444444";
const availableId = "55555555-5555-4555-8555-555555555555";
const dmCharacterId = "66666666-6666-4666-8666-666666666666";
function event(fetch, fields = {}, token = "signed-token") {
  const body = new FormData();
  for (const [key, value] of Object.entries(fields)) body.set(key, value);
  return {
    fetch,
    cookies: {
      get: (name) => (name === "grimoire-id-token" ? token : undefined),
    },
    setHeaders: vi.fn(),
    request: new Request("https://friends.jomcgi.dev/grimoire", {
      method: "POST",
      body,
      headers: {
        authorization: "Bearer unrelated",
        "x-auth-email": "impostor@example.test",
        "x-grimoire-token": "forged",
      },
    }),
  };
}
const response = (body, status = 200) => ({
  ok: status < 400,
  status,
  json: async () => body,
});

function expectJsonRequest(fetch, path, method, body) {
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0]).toEqual([
    `http://backend.test/api/grimoire${path}`,
    {
      method,
      body: JSON.stringify(body),
      headers: {
        "content-type": "application/json",
        "x-grimoire-token": "signed-token",
      },
      signal: expect.any(AbortSignal),
    },
  ]);
}

async function renderLobby(campaign) {
  const { html } = await render(Lobby, {
    props: {
      data: {
        user: { email: "player@example.test" },
        campaigns: [campaign],
        invitations: [],
      },
    },
  });
  return html.replace(/\s+/g, " ");
}

beforeEach(() => {
  process.env.API_BASE = "http://backend.test";
});

describe("Grimoire lobby", () => {
  it("forwards only the app cookie and does not fetch owner data for players", async () => {
    const fetch = vi.fn().mockResolvedValue(
      response({
        user: { email: "player@example.test" },
        campaigns: [{ id: campaignId, role: "player", is_owner: false }],
        invitations: [],
      }),
    );
    const request = event(fetch);
    await load(request);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][1].headers).toEqual({
      "x-grimoire-token": "signed-token",
    });
    expect(request.setHeaders).toHaveBeenCalledWith({
      "cache-control": "private, no-store",
    });
  });

  it.each([true, false])(
    "loads DM data with owner=%s and excludes every held PC",
    async (is_owner) => {
      const members = [
        { id: memberId, role: "player", player_character_id: characterId },
        { id: "dm", role: "dm", player_character_id: dmCharacterId },
        { id: "unseated", role: "player", player_character_id: null },
      ];
      const available = { id: availableId, character_name: "Mira" };
      const invitations = [{ id: invitationId }];
      const fetch = vi
        .fn()
        .mockResolvedValueOnce(
          response({ campaigns: [{ id: campaignId, role: "dm", is_owner }] }),
        )
        .mockResolvedValueOnce(response(members))
        .mockResolvedValueOnce(
          response([{ id: characterId }, available, { id: dmCharacterId }]),
        )
        .mockResolvedValueOnce(response(invitations));
      const lobby = await load(event(fetch));
      expect(fetch.mock.calls.map(([url]) => url)).toEqual([
        "http://backend.test/api/grimoire/lobby",
        `http://backend.test/api/grimoire/campaigns/${campaignId}/members`,
        `http://backend.test/api/grimoire/campaigns/${campaignId}/characters`,
        ...(is_owner
          ? [
              `http://backend.test/api/grimoire/campaigns/${campaignId}/invitations`,
            ]
          : []),
      ]);
      expect(lobby.campaigns[0]).toEqual({
        id: campaignId,
        role: "dm",
        is_owner,
        needs_character: false,
        members,
        unassigned_characters: [available],
        invitations: is_owner ? invitations : [],
      });
      for (const [, options] of fetch.mock.calls)
        expect(options.headers).toEqual({ "x-grimoire-token": "signed-token" });
    },
  );

  it.each([null, characterId])(
    "loads only the lobby for a player with PC %s",
    async (player_character_id) => {
      const campaign = {
        id: campaignId,
        role: "player",
        is_owner: false,
        player_character_id,
        character_name: player_character_id ? "Mira" : null,
      };
      const fetch = vi
        .fn()
        .mockResolvedValue(response({ campaigns: [campaign] }));
      const lobby = await load(event(fetch));
      expect(fetch).toHaveBeenCalledTimes(1);
      expect(fetch.mock.calls[0][0]).toBe(
        "http://backend.test/api/grimoire/lobby",
      );
      expect(lobby.campaigns).toEqual([
        { ...campaign, needs_character: !player_character_id },
      ]);
    },
  );

  it("fails closed when the app cookie is absent", async () => {
    const fetch = vi.fn();
    await expect(load(event(fetch, {}, null))).rejects.toThrow(
      "session has expired",
    );
    expect(fetch).not.toHaveBeenCalled();
  });

  it("invites by exact email without passing roles or user IDs", async () => {
    const fetch = vi.fn().mockResolvedValue(response({ id: invitationId }));
    const result = await actions.invite(
      event(fetch, {
        campaign_id: campaignId,
        email: "friend@example.test",
        role: "dm",
        app_user_id: "forged",
      }),
    );
    expect(result.ok).toBe(true);
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      email: "friend@example.test",
    });
  });

  it("cannot turn an action into an arbitrary API proxy", async () => {
    const fetch = vi.fn();
    const result = await actions.accept(
      event(fetch, { invitation_id: "../../alias-candidates/execute" }),
    );
    expect(result.status).toBe(400);
    expect(fetch).not.toHaveBeenCalled();
  });

  it("surfaces rejected or already consumed invitations", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        response({ detail: "invitation is no longer pending" }, 409),
      );
    const result = await actions.accept(
      event(fetch, { invitation_id: invitationId }),
    );
    expect(result.data.error).toBe("invitation is no longer pending");
  });

  it.each([
    [
      "existing",
      { player_character_id: characterId },
      { player_character_id: characterId },
    ],
    ["new", { name: "  Mira  " }, { new: { name: "Mira" } }],
    ["clear", {}, { player_character_id: null }],
  ])(
    "assigns with exact %s wire contract and cookie-only identity",
    async (mode, fields, body) => {
      const fetch = vi.fn().mockResolvedValue(response({ id: memberId }));
      const result = await actions.assign(
        event(fetch, {
          campaign_id: campaignId,
          member_id: memberId,
          mode,
          ...fields,
          role: "dm",
          app_user_id: "forged",
        }),
      );
      expect(result).toEqual({ ok: true });
      expectJsonRequest(
        fetch,
        `/campaigns/${campaignId}/members/${memberId}/character`,
        "PUT",
        body,
      );
    },
  );

  it.each([
    { campaign_id: "../../campaigns" },
    { member_id: "not-a-uuid" },
    { player_character_id: "not-a-uuid" },
    { mode: "new", name: "  " },
    { mode: "new", name: "x".repeat(121) },
    { mode: "unknown" },
  ])("refuses invalid assignment %j before fetch", async (fields) => {
    const fetch = vi.fn();
    const result = await actions.assign(
      event(fetch, {
        campaign_id: campaignId,
        member_id: memberId,
        mode: "existing",
        player_character_id: characterId,
        ...fields,
      }),
    );
    expect(result.status).toBe(400);
    expect(result.data.error).toBeTruthy();
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each([403, 404, 409])(
    "maps assignment backend %s to fail(400) with its message",
    async (status) => {
      const fetch = vi
        .fn()
        .mockResolvedValue(
          response({ detail: "character already assigned" }, status),
        );
      const result = await actions.assign(
        event(fetch, {
          campaign_id: campaignId,
          member_id: memberId,
          mode: "existing",
          player_character_id: characterId,
        }),
      );
      expect(result.status).toBe(400);
      expect(result.data).toEqual({ error: "character already assigned" });
    },
  );

  it("posts a self-created character then redirects to the sheet editor", async () => {
    let resolveResponse;
    const fetch = vi.fn().mockReturnValue(
      new Promise((resolve) => {
        resolveResponse = resolve;
      }),
    );
    let settled = false;
    const result = actions
      .createCharacter(
        event(fetch, {
          campaign_id: campaignId,
          name: "  Mira  ",
          player_character_id: "forged",
          role: "dm",
        }),
      )
      .catch((error) => {
        settled = true;
        return error;
      });
    // The form must not redirect before the creation request completes.
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    expect(settled).toBe(false);
    resolveResponse(response({ id: characterId }));
    expect(await result).toMatchObject({
      status: 303,
      location: "/grimoire/sheets",
    });
    expectJsonRequest(
      fetch,
      `/campaigns/${campaignId}/characters/self`,
      "POST",
      { name: "Mira" },
    );
  });

  it.each([
    { campaign_id: "not-a-uuid" },
    { name: "  " },
    { name: "x".repeat(121) },
  ])("refuses invalid self creation %j before fetch", async (fields) => {
    const fetch = vi.fn();
    const result = await actions.createCharacter(
      event(fetch, { campaign_id: campaignId, name: "Mira", ...fields }),
    );
    expect(result.status).toBe(400);
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each([403, 404, 409])(
    "maps self creation backend %s to fail(400) without redirecting",
    async (status) => {
      const fetch = vi
        .fn()
        .mockResolvedValue(
          response({ detail: "player already has a character" }, status),
        );
      const result = await actions.createCharacter(
        event(fetch, { campaign_id: campaignId, name: "Mira" }),
      );
      expect(result.status).toBe(400);
      expect(result.data).toEqual({ error: "player already has a character" });
    },
  );

  it.each(["assign", "createCharacter"])(
    "fails closed without an app cookie for %s",
    async (actionName) => {
      const fetch = vi.fn();
      const result = await actions[actionName](
        event(
          fetch,
          {
            campaign_id: campaignId,
            member_id: memberId,
            mode: "clear",
            name: "Mira",
          },
          null,
        ),
      );
      expect(result.status).toBe(400);
      expect(result.data.error).toContain("session has expired");
      expect(fetch).not.toHaveBeenCalled();
    },
  );

  it.each([true, false])(
    "renders DM assignment controls with owner=%s",
    async (is_owner) => {
      const html = await renderLobby({
        id: campaignId,
        name: "Adventure",
        role: "dm",
        is_owner,
        members: [
          {
            id: memberId,
            email: "seated@example.test",
            role: "player",
            player_character_id: characterId,
            character_name: "Rowan",
          },
          {
            id: invitationId,
            email: "unseated@example.test",
            role: "player",
            player_character_id: null,
            character_name: null,
          },
        ],
        unassigned_characters: [{ id: availableId, character_name: "Mira" }],
        invitations: [],
      });
      expect(html).toContain("Rowan");
      expect(html).toContain(`href="/grimoire/campaigns/${campaignId}/notes"`);
      expect(html).toContain("No character");
      expect(
        html.match(/<summary[^>]*>Assign character<\/summary>/g),
      ).toHaveLength(2);
      expect(html.match(/action="\?\/assign"/g)).toHaveLength(5);
      expect(html).toContain(`name="campaign_id" value="${campaignId}"`);
      expect(html).toContain(`name="member_id" value="${memberId}"`);
      expect(html).toContain('name="mode" value="existing"');
      expect(html).toContain('name="mode" value="new"');
      expect(html.match(/name="mode" value="clear"/g)).toHaveLength(1);
      expect(html).toContain(`value="${availableId}">Mira</option>`);
      expect(html).not.toContain(`value="${characterId}">`);
      expect(html).toContain(
        'Unassigned character <select name="player_character_id" required=""',
      );
      expect(html).toContain(
        'New character name <input name="name" required="" maxlength="120"',
      );
      expect(html.includes('action="?/invite"')).toBe(is_owner);
      expect(html.includes('action="?/remove"')).toBe(is_owner);
      expect(html).not.toContain('action="?/createCharacter"');
    },
  );

  it.each([true, false])(
    "renders the player character state needs_character=%s",
    async (needs_character) => {
      const html = await renderLobby({
        id: campaignId,
        role: "player",
        is_owner: false,
        needs_character,
        character_name: needs_character ? null : "Mira",
      });
      expect(html.includes('action="?/createCharacter"')).toBe(needs_character);
      expect(html.includes("Create your character")).toBe(needs_character);
      expect(html.includes("Your character: Mira")).toBe(!needs_character);
      expect(html).toContain('href="/grimoire/sheets"');
      expect(html).not.toContain("Assign character");
      expect(html).not.toContain('action="?/invite"');
      if (needs_character) {
        expect(html).toContain(`name="campaign_id" value="${campaignId}"`);
        expect(html).toContain(
          'Character name <input name="name" required="" maxlength="120"',
        );
      }
    },
  );

  it("shows a useful DM empty state without an empty character picker", async () => {
    const html = await renderLobby({
      id: campaignId,
      role: "dm",
      is_owner: false,
      members: [{ id: memberId, role: "player", player_character_id: null }],
      unassigned_characters: [],
      invitations: [],
    });
    expect(html).toContain("No unassigned characters. Create one below.");
    expect(html).not.toContain('<select name="player_character_id"');
    expect(html).toContain("Create and assign character");
    expect(html).not.toContain("Clear character assignment");
  });
});
