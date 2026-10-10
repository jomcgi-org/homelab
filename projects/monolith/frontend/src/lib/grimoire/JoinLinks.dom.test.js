// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import JoinLinks from "./JoinLinks.svelte";

const id = "11111111-1111-4111-8111-111111111111";
const link = {
  campaign_id: id,
  invitee_email: "friend@example.test",
  expires_at: "2026-10-06T12:00:00Z",
  url: `https://friends.jomcgi.dev/grimoire/join#${"T".repeat(43)}`,
};
let instance;
async function render(props = {}) {
  instance = mount(JoinLinks, {
    target: document.body,
    props: { campaign: { id, join_links: [] }, ...props },
  });
  await tick();
}
afterEach(async () => {
  if (instance) await unmount(instance);
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("owner invitation links", () => {
  it.each([
    [true, true],
    [false, true],
    [true, false],
    [false, false],
  ])(
    "only offers account creation for enabled admins (%s, %s)",
    async (isAdmin, canEnroll) => {
      await render({ isAdmin, canEnroll });
      expect(Boolean(document.querySelector('[name="allow_enrollment"]'))).toBe(
        isAdmin && canEnroll,
      );
      expect(document.body.textContent).not.toContain("Send via Email");
    },
  );

  it("blocks repeated creates while a request is pending", async () => {
    await render();
    const form = document.querySelector("form");
    const first = new Event("submit", { bubbles: true, cancelable: true });
    const second = new Event("submit", { bubbles: true, cancelable: true });
    form.dispatchEvent(first);
    form.dispatchEvent(second);
    await tick();
    expect(first.defaultPrevented).toBe(false);
    expect(second.defaultPrevented).toBe(true);
    expect(form.querySelector("button").disabled).toBe(true);
  });

  it("copies only the freshly-created link", async () => {
    const copy = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    await render({ form: { new_link: link } });
    [...document.querySelectorAll("button")]
      .find((b) => b.textContent === "Copy link")
      .click();
    await tick();
    expect(copy).toHaveBeenCalledWith(link.url);
    await vi.waitFor(() =>
      expect(document.body.textContent).toContain("Link copied"),
    );
  });

  it("falls back to selecting the link when clipboard access fails", async () => {
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(
      new Error("Unavailable"),
    );
    await render({ form: { new_link: link } });
    [...document.querySelectorAll("button")]
      .find((b) => b.textContent === "Copy link")
      .click();
    await tick();
    await tick();
    const input = document.querySelector("input[readonly]");
    expect(document.activeElement).toBe(input);
    expect(input.selectionEnd).toBe(link.url.length);
    expect(document.body.textContent).toContain("copy it manually");
  });

  it("hides the secret on dismissal and before a back-forward cache restore", async () => {
    await render({ form: { new_link: link } });
    expect(document.querySelector("input[readonly]").value).toBe(link.url);
    window.dispatchEvent(new Event("pagehide"));
    await tick();
    expect(document.querySelector("input[readonly]")).toBeNull();
    window.dispatchEvent(new Event("pageshow"));
    await tick();
    expect(document.body.innerHTML).not.toContain(link.url);
  });

  it("allows provider cleanup retry after the campaign link has been revoked", async () => {
    await render({
      campaign: {
        id,
        join_links: [
          {
            id: "link-id",
            invitee_email: "friend@example.test",
            expires_at: link.expires_at,
            status: "revoked",
            enrollment_cleanup_pending: true,
          },
        ],
      },
    });
    expect(document.body.textContent).toContain("revoked");
    expect(document.body.textContent).toContain(
      "Retry account invitation cleanup",
    );
    expect(
      document.querySelector('form[action="?/revokeLink"]'),
    ).not.toBeNull();
  });

  it("never reconstructs a link from listed metadata or another campaign's action", async () => {
    await render({
      campaign: {
        id,
        join_links: [
          {
            id: "link-id",
            invitee_email: "friend@example.test",
            expires_at: link.expires_at,
            status: "pending",
            token: "SECRET-MUST-NOT-RENDER",
          },
        ],
      },
      form: { new_link: { ...link, campaign_id: "other" } },
    });
    expect(document.body.innerHTML).not.toContain("SECRET-MUST-NOT-RENDER");
    expect(document.querySelector("input[readonly]")).toBeNull();
    expect(document.body.textContent).toContain("Revoke link");
  });
});
