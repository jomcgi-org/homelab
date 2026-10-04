import { beforeEach, describe, expect, it, vi } from "vitest";
import { GET, POST } from "./+server.js";
import { JOIN_COOKIE, enrollmentUrl } from "$lib/server/grimoire-join-links.js";

const token = "T".repeat(43);
const metadata = {
  id: "22222222-2222-4222-8222-222222222222",
  campaign_name: "Adventure",
  invitee_email: "friend@example.test",
  expires_at: "2026-10-06T12:00:00Z",
  status: "pending",
  can_enroll: true,
};
function event(body, options = {}) {
  body = { invitation_id: metadata.id, ...body };
  const url = new URL("https://friends.jomcgi.dev/grimoire/join");
  return {
    url,
    cookies: {
      get: vi.fn(() => options.cookie ?? token),
      set: vi.fn(),
      delete: vi.fn(),
    },
    fetch: vi.fn().mockImplementation(async (url) => {
      const checking = body.action === "enroll" && url.endsWith("/inspect");
      return new Response(
        JSON.stringify(
          checking
            ? (options.inspected ?? metadata)
            : (options.response ?? {
                ...metadata,
                token,
                provider_invitation_id: "provider-secret",
              }),
        ),
        {
          status: checking ? 200 : (options.status ?? 200),
          headers: { "content-type": "application/json" },
        },
      );
    }),
    request: new Request(url, {
      method: "POST",
      headers: {
        origin: options.origin ?? url.origin,
        "content-type": options.form
          ? "application/x-www-form-urlencoded"
          : "application/json",
      },
      body: options.form ? new URLSearchParams(body) : JSON.stringify(body),
    }),
  };
}
beforeEach(() => {
  process.env.API_BASE = "http://backend.test";
  process.env.GRIMOIRE_INVITATION_LINKS_ENABLED = "true";
});

describe("public invitation landing", () => {
  it("is self-contained and does not include authenticated assets or telemetry", async () => {
    const result = GET();
    const html = await result.text();
    expect(html).not.toContain('src="');
    expect(html).not.toContain("_app/");
    expect(html).not.toContain("otel");
    expect(result.headers.get("cache-control")).toContain("no-store");
    expect(result.headers.get("referrer-policy")).toBe("no-referrer");
    expect(result.headers.get("content-security-policy")).toContain(
      "default-src 'none'",
    );
    expect(result.headers.get("content-security-policy")).not.toContain(
      "unsafe-inline",
    );
  });

  it("is disabled without a flag and clears any fragment without making requests", async () => {
    delete process.env.GRIMOIRE_INVITATION_LINKS_ENABLED;
    const result = GET();
    expect(result.status).toBe(503);
    const html = await result.text();
    expect(html).toContain("unavailable");
    expect(html).toContain("history.replaceState");
    expect(html).not.toContain("fetch(");
    const request = event({ action: "capture", token });
    expect((await POST(request)).status).toBe(503);
    expect(request.fetch).not.toHaveBeenCalled();
  });

  it("captures the bearer in a secure resume cookie and serializes only metadata", async () => {
    const request = event({ action: "capture", token });
    const response = await POST(request);
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body).toEqual(metadata);
    expect(JSON.stringify(body)).not.toContain(token);
    expect(JSON.stringify(body)).not.toContain("provider-secret");
    expect(request.cookies.set).toHaveBeenCalledWith(JOIN_COOKIE, token, {
      path: "/grimoire/join",
      httpOnly: true,
      secure: true,
      sameSite: "lax",
      maxAge: 3600,
    });
    expect(request.cookies.delete).toHaveBeenCalled();
    expect(request.fetch.mock.calls[0][1].headers).toEqual({
      "content-type": "application/json",
    });
    expect(JSON.parse(request.fetch.mock.calls[0][1].body)).toEqual({ token });
  });

  it.each(["", "too-short", "../../unsafe", "T".repeat(129)])(
    "clears stale resume state on malformed new token %s",
    async (value) => {
      const request = event({ action: "capture", token: value });
      expect((await POST(request)).status).toBe(400);
      expect(request.cookies.delete).toHaveBeenCalled();
      expect(request.cookies.set).not.toHaveBeenCalled();
      expect(request.fetch).not.toHaveBeenCalled();
    },
  );

  it("rejects cross-origin capture without reading or changing an invitation", async () => {
    const request = event(
      { action: "capture", token },
      { origin: "https://evil.test" },
    );
    expect((await POST(request)).status).toBe(403);
    expect(request.fetch).not.toHaveBeenCalled();
    expect(request.cookies.set).not.toHaveBeenCalled();
  });

  it("resumes from the cookie, ignoring an alternate token in inspect", async () => {
    const request = event({ action: "inspect", token: "S".repeat(43) });
    expect((await POST(request)).status).toBe(200);
    expect(JSON.parse(request.fetch.mock.calls[0][1].body)).toEqual({ token });
  });

  it.each(["accepted", "expired", "revoked"])(
    "does not keep a %s link usable",
    async (status) => {
      const request = event(
        { action: "capture", token },
        { response: { ...metadata, status } },
      );
      expect((await POST(request)).status).toBe(200);
      expect(request.cookies.delete).toHaveBeenCalled();
      expect(request.cookies.set).not.toHaveBeenCalled();
    },
  );

  it("never reflects backend error details that might contain secrets", async () => {
    const request = event(
      { action: "capture", token },
      { response: { detail: token }, status: 500 },
    );
    const result = await POST(request);
    expect(await result.text()).not.toContain(token);
    expect(result.status).toBe(400);
  });

  it("blocks an older tab from enrolling with a newer tab's cookie", async () => {
    const request = event(
      {
        action: "enroll",
        invitation_id: "33333333-3333-4333-8333-333333333333",
      },
      { form: true },
    );
    const result = await POST(request);
    expect(await result.text()).toContain("Invitation changed");
    expect(request.fetch).toHaveBeenCalledTimes(1);
    expect(request.fetch.mock.calls[0][0]).toContain("/inspect");
    expect(request.cookies.delete).not.toHaveBeenCalled();
  });

  it("uses one captured cookie token for enrollment inspection and enrollment", async () => {
    const request = event(
      { action: "enroll" },
      {
        form: true,
        response: {
          enrollment_url:
            "https://auth.jomcgi.dev/if/flow/grimoire-link-enrollment/?itoken=22222222-2222-4222-8222-222222222222",
        },
      },
    );
    request.cookies.get
      .mockReturnValueOnce(token)
      .mockReturnValue("S".repeat(43));
    expect((await POST(request)).status).toBe(303);
    expect(request.cookies.get).toHaveBeenCalledTimes(1);
    expect(
      request.fetch.mock.calls.map(([, options]) => JSON.parse(options.body)),
    ).toEqual([{ token }, { token }]);
  });

  it("keeps resume state and offers sign-in after interrupted enrollment", async () => {
    const request = event(
      { action: "enroll" },
      {
        form: true,
        status: 409,
        response: { detail: "An account already exists. Please sign in." },
      },
    );
    const result = await POST(request);
    const html = await result.text();
    expect(html).toContain("An account may already exist");
    expect(html).toContain('href="/grimoire/join/accept">Sign in to continue');
    expect(request.cookies.delete).not.toHaveBeenCalled();
    expect(html).not.toContain("Ask the campaign owner for a new link");
  });

  it("does not expose credentials from an unexpected transport error", async () => {
    const request = event({ action: "capture", token });
    request.fetch.mockRejectedValue(
      new TypeError("Cannot fetch https://user:secret@backend.test"),
    );
    const result = await POST(request);
    const body = await result.text();
    expect(body).toContain("unavailable");
    expect(body).not.toContain("secret");
    expect(body).not.toContain("backend.test");
  });

  it("closes without signing an anonymous visitor in", async () => {
    const request = event({ action: "cancel" }, { form: true });
    const result = await POST(request);
    expect(request.cookies.delete).toHaveBeenCalled();
    expect(request.fetch).not.toHaveBeenCalled();
    expect(await result.text()).toContain("Invitation closed");
    expect(result.headers.has("location")).toBe(false);
  });

  it("only redirects enrollment to the dedicated authentik flow", async () => {
    const enrollment_url =
      "https://auth.jomcgi.dev/if/flow/grimoire-link-enrollment/?itoken=22222222-2222-4222-8222-222222222222";
    const request = event(
      { action: "enroll" },
      { form: true, response: { enrollment_url } },
    );
    const result = await POST(request);
    expect(result.status).toBe(303);
    expect(result.headers.get("location")).toBe(enrollment_url);
    expect(result.headers.get("cache-control")).toContain("no-store");
    expect(request.fetch.mock.calls[1][0]).toBe(
      "http://backend.test/api/grimoire/join-links/enroll",
    );
  });

  it.each([
    "https://evil.test/",
    "https://auth.jomcgi.dev/if/flow/grimoire-link-enrollment/",
    "https://auth.jomcgi.dev/if/flow/grimoire-link-enrollment/?itoken=invalid",
    "https://auth.jomcgi.dev/if/flow/grimoire-link-enrollment/?itoken=22222222-2222-4222-8222-222222222222&itoken=33333333-3333-4333-8333-333333333333",
    "https://auth.jomcgi.dev/if/flow/grimoire-link-enrollment/?itoken=22222222-2222-4222-8222-222222222222&next=https://evil.test",
    "https://auth.jomcgi.dev/if/flow/grimoire-link-enrollment/?itoken=22222222-2222-4222-8222-222222222222&redirect=https://evil.test",
    "http://auth.jomcgi.dev/if/flow/grimoire-link-enrollment/",
    "https://auth.jomcgi.dev/if/flow/other/",
    "https://user@auth.jomcgi.dev/if/flow/grimoire-link-enrollment/",
    "https://auth.jomcgi.dev/if/flow/grimoire-link-enrollment/#secret",
  ])("rejects enrollment redirect %s", (url) => {
    expect(() => enrollmentUrl(url)).toThrow();
  });
});
