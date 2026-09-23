import { beforeEach, describe, expect, it, vi } from "vitest";
import { actions, load } from "./+page.server.js";

const campaignId = "11111111-1111-4111-8111-111111111111";
const invitationId = "22222222-2222-4222-8222-222222222222";
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

beforeEach(() => {
  process.env.API_BASE = "http://backend.test";
});

describe("Grimoire lobby", () => {
  it("forwards only the app cookie and does not fetch owner data for players", async () => {
    const fetch = vi.fn().mockResolvedValue(
      response({
        user: { email: "player@example.test" },
        campaigns: [{ id: campaignId, is_owner: false }],
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

  it("loads owner membership and pending invitations", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        response({ campaigns: [{ id: campaignId, is_owner: true }] }),
      )
      .mockResolvedValue(response([]));
    const lobby = await load(event(fetch));
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      "http://backend.test/api/grimoire/lobby",
      `http://backend.test/api/grimoire/campaigns/${campaignId}/members`,
      `http://backend.test/api/grimoire/campaigns/${campaignId}/invitations`,
    ]);
    expect(lobby.campaigns[0].members).toEqual([]);
  });

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
});
