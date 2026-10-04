import { beforeEach, describe, expect, it, vi } from "vitest";
import { render } from "svelte/server";
import Accept from "./+page.svelte";
import { actions, load } from "./+page.server.js";
import { JOIN_COOKIE } from "$lib/server/grimoire-join-links.js";

const token = "T".repeat(43);
const invitation = {
  id: "22222222-2222-4222-8222-222222222222",
  campaign_name: "Adventure",
  invitee_email: "friend@example.test",
  expires_at: "2026-10-06T12:00:00Z",
  status: "pending",
};
const response = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
function event(
  fetch,
  bearer = token,
  signed = "signed-id-token",
  selected = invitation.id,
) {
  const body = new FormData();
  body.set("invitation_id", selected);
  return {
    request: new Request("https://friends.jomcgi.dev/grimoire/join/accept", {
      method: "POST",
      body,
    }),
    fetch,
    cookies: {
      get: vi.fn((name) => (name === JOIN_COOKIE ? bearer : signed)),
      delete: vi.fn(),
    },
    setHeaders: vi.fn(),
  };
}
beforeEach(() => {
  process.env.API_BASE = "http://backend.test";
  process.env.GRIMOIRE_INVITATION_LINKS_ENABLED = "true";
});

describe("authenticated invitation acceptance", () => {
  it("loads only safe metadata and matches the signed-in recipient", async () => {
    const fetch = vi.fn().mockImplementation(async (url) =>
      url.endsWith("/lobby")
        ? response({
            user: { email: "FRIEND@example.test" },
            invitation_links_enabled: true,
          })
        : response({
            ...invitation,
            token,
            provider_invitation_id: "provider-secret",
          }),
    );
    const data = await load(event(fetch));
    expect(data.matches).toBe(true);
    expect(JSON.stringify(data)).not.toContain(token);
    expect(JSON.stringify(data)).not.toContain("provider-secret");
    expect(fetch.mock.calls[0][1].headers).toEqual({
      "x-grimoire-token": "signed-id-token",
    });
  });

  it("keeps the invitation during an expired identity session", async () => {
    const request = event(
      vi.fn().mockResolvedValue(response(invitation)),
      token,
      null,
    );
    const data = await load(request);
    expect(data.error).toContain("session has expired");
    expect(request.cookies.delete).not.toHaveBeenCalled();
  });

  it("does not redeem without the frontend flag", async () => {
    delete process.env.GRIMOIRE_INVITATION_LINKS_ENABLED;
    const request = event(vi.fn());
    const result = await actions.accept(request);
    expect(result.data.error).toContain("unavailable");
    expect(request.fetch).not.toHaveBeenCalled();
  });

  it("requires explicit accept, forwards only the signed identity, then clears the resume cookie", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response(invitation))
      .mockResolvedValue(
        response({
          campaign_id: "campaign-id",
          status: "accepted",
          redirect: "https://evil.test",
        }),
      );
    const request = event(fetch);
    await expect(actions.accept(request)).rejects.toMatchObject({
      status: 303,
      location: "/grimoire",
    });
    expect(fetch.mock.calls[1][0]).toBe(
      "http://backend.test/api/grimoire/join-links/redeem",
    );
    expect(fetch.mock.calls[1][1].headers).toEqual({
      "content-type": "application/json",
      "x-grimoire-token": "signed-id-token",
    });
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ token });
    expect(request.cookies.delete).toHaveBeenCalledWith(
      JOIN_COOKIE,
      expect.objectContaining({ path: "/grimoire/join" }),
    );
  });

  it("rejects expired or mismatched redemption without clearing recoverable state", async () => {
    const request = event(
      vi
        .fn()
        .mockResolvedValueOnce(response(invitation))
        .mockResolvedValue(
          response(
            {
              detail:
                "Sign in with the account this invitation was created for.",
            },
            403,
          ),
        ),
    );
    const result = await actions.accept(request);
    expect(result.status).toBe(400);
    expect(result.data.error).toContain("account this invitation");
    expect(request.cookies.delete).not.toHaveBeenCalled();
  });

  it("keeps email mismatch advisory and lets the backend check the immutable identity", async () => {
    const { html } = await render(Accept, {
      props: {
        data: { invitation, email: "other@example.test", matches: false },
      },
    });
    expect(html).toContain("differs from your current account email");
    expect(html).toContain('action="?/accept"');
    expect(html).toContain('action="?/cancel"');
  });

  it("blocks an older tab from redeeming a newer invitation and never rereads the bearer", async () => {
    const fetch = vi.fn().mockResolvedValue(response(invitation));
    const request = event(
      fetch,
      token,
      "signed-id-token",
      "33333333-3333-4333-8333-333333333333",
    );
    const result = await actions.accept(request);
    expect(result.data.error).toContain("Invitation changed");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toContain("/inspect");
    expect(request.cookies.delete).not.toHaveBeenCalled();
    expect(
      request.cookies.get.mock.calls.filter(([name]) => name === JOIN_COOKIE),
    ).toHaveLength(1);
  });

  it("does not expose upstream URLs or unknown backend errors from load or accept", async () => {
    const request = event(
      vi
        .fn()
        .mockRejectedValue(
          new TypeError("Cannot fetch https://user:secret@backend.test"),
        ),
    );
    expect((await load(request)).error).toContain("unavailable");
    expect(JSON.stringify(await actions.accept(request))).not.toContain(
      "secret",
    );
    const rejected = event(
      vi.fn().mockResolvedValue(response({ detail: token }, 500)),
    );
    expect(JSON.stringify(await actions.accept(rejected))).not.toContain(token);
  });

  it("cancels the resume cookie without a backend mutation", async () => {
    const request = event(vi.fn());
    await expect(actions.cancel(request)).rejects.toMatchObject({
      status: 303,
      location: "/grimoire",
    });
    expect(request.cookies.delete).toHaveBeenCalled();
    expect(request.fetch).not.toHaveBeenCalled();
  });
});
