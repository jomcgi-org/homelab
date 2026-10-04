// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { startJoinLanding } from "./join-landing.js";

const token = "T".repeat(43);
const metadata = {
  id: "22222222-2222-4222-8222-222222222222",
  campaign_name: "Adventure",
  invitee_email: "friend@example.test",
  expires_at: "2026-10-06T12:00:00Z",
  status: "pending",
  can_enroll: true,
};
beforeEach(() => {
  document.body.innerHTML =
    '<p id="status"></p><section id="invitation"><h2 id="campaign"></h2><span id="recipient"></span><span id="expires"></span><span id="link-status"></span></section><div id="actions"><a id="sign-in" href="/grimoire/join/accept">Sign in to accept</a><form id="enrollment"><input id="enrollment-invitation" name="invitation_id" type="hidden"><button>Create account</button></form></div>';
  window.history.replaceState(null, "", `/grimoire/join#${token}`);
});
afterEach(() => {
  vi.unstubAllGlobals();
  document.body.innerHTML = "";
});

describe("fragment handoff", () => {
  it("clears the fragment before fetching, then shows escaped metadata without a token", async () => {
    const fetch = vi.fn(async (_url, options) => {
      expect(window.location.hash).toBe("");
      expect(JSON.parse(options.body)).toEqual({ action: "capture", token });
      return new Response(
        JSON.stringify({
          ...metadata,
          campaign_name: "<img src=x onerror=alert(1)>",
        }),
      );
    });
    vi.stubGlobal("fetch", fetch);
    await startJoinLanding();
    expect(document.querySelector("img")).toBeNull();
    expect(document.getElementById("campaign").textContent).toContain("<img");
    expect(document.getElementById("actions").hidden).toBe(false);
    expect(document.body.innerHTML).not.toContain(token);
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it("resumes from the cookie after navigation, without putting a token in the URL", async () => {
    window.history.replaceState(null, "", "/grimoire/join");
    const fetch = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(metadata)));
    vi.stubGlobal("fetch", fetch);
    await startJoinLanding();
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      action: "inspect",
    });
    expect(window.location.hash).toBe("");
  });

  it("clears even an invalid fragment and keeps actions hidden on rejection", async () => {
    window.history.replaceState(null, "", "/grimoire/join#wrong");
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(
            JSON.stringify({ error: "This invitation is incomplete." }),
            { status: 400 },
          ),
        ),
    );
    await startJoinLanding();
    expect(window.location.hash).toBe("");
    expect(document.getElementById("status").getAttribute("role")).toBe(
      "alert",
    );
    expect(document.getElementById("actions").hidden).toBe(true);
  });

  it.each(["expired", "revoked"])(
    "never offers acceptance for status %s",
    async (status) => {
      vi.stubGlobal(
        "fetch",
        vi
          .fn()
          .mockResolvedValue(
            new Response(JSON.stringify({ ...metadata, status })),
          ),
      );
      await startJoinLanding();
      expect(document.getElementById("actions").hidden).toBe(true);
      expect(document.getElementById("status").textContent).toContain(
        "no longer available",
      );
    },
  );

  it("sends an already-accepted player to their campaigns without replaying redemption", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ ...metadata, status: "accepted" })),
        ),
    );
    await startJoinLanding();
    expect(document.getElementById("sign-in").getAttribute("href")).toBe(
      "/grimoire",
    );
    expect(document.getElementById("enrollment").hidden).toBe(true);
  });

  it("keeps enrollment hidden for an existing-account-only invitation", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(JSON.stringify({ ...metadata, can_enroll: false })),
        ),
    );
    await startJoinLanding();
    expect(document.getElementById("enrollment").hidden).toBe(true);
  });

  it("blocks repeated enrollment submissions while the first is pending", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify(metadata))),
    );
    await startJoinLanding();
    const form = document.getElementById("enrollment");
    const first = new Event("submit", { cancelable: true });
    const second = new Event("submit", { cancelable: true });
    form.dispatchEvent(first);
    form.dispatchEvent(second);
    expect(first.defaultPrevented).toBe(false);
    expect(second.defaultPrevented).toBe(true);
    expect(form.querySelector("button").disabled).toBe(true);
  });
});
