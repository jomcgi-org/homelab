// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Session from "../../routes/friends/grimoire/campaigns/[id]/session/+page.svelte";

let component, target, next, posts;
const data = () => ({
  rolloutRecoveryEnabled: true,
  campaign: { id: "campaign", name: "Recovery table", role: "dm" },
  user: { id: "dm" },
  session: { id: "session", status: "active" },
  characters: [],
  events: [],
  voices: [],
  members: [],
  initiative: null,
});
const settle = async () => {
  await tick();
  await Promise.resolve();
  await tick();
};
async function poll() {
  document.dispatchEvent(new Event("visibilitychange"));
  await settle();
}
async function show(enabled = true) {
  component = mount(Session, {
    target,
    props: { data: { ...data(), rolloutRecoveryEnabled: enabled } },
  });
  await settle();
}
function send() {
  const input = target.querySelector("#message");
  input.value = "Hold the door";
  input.dispatchEvent(new Event("input", { bubbles: true }));
  input
    .closest("form")
    .dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
}
beforeEach(() => {
  vi.useFakeTimers();
  sessionStorage.clear();
  target = document.createElement("div");
  document.body.append(target);
  posts = [];
  next = () => new Response(JSON.stringify(data()));
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url, options) => {
      if (options?.method === "POST") {
        posts.push(JSON.parse(options.body));
        return next();
      }
      if (String(url).includes("?notes=")) return new Response("[]");
      return next();
    }),
  );
});
afterEach(async () => {
  if (component) await unmount(component);
  component = null;
  target.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
it.each([502, 503, 504, "connection"])(
  "keeps an uncertain POST on %s for manual deduplicated retry without an error banner",
  async (status) => {
    await show();
    next = () => {
      if (status === "connection") throw new TypeError("Failed to fetch");
      return new Response("<html>proxy restarting</html>", { status });
    };
    send();
    await settle();
    expect(
      target.querySelector('[aria-label="Session connection"]').textContent,
    ).toBe("Reconnecting");
    expect(target.querySelector('[role="alert"]')).toBeNull();
    expect(target.querySelector("#message").value).toBe("Hold the door");
    expect(posts).toHaveLength(1);
    send();
    await settle();
    expect(posts).toHaveLength(2);
    expect(posts[1].requestId).toBe(posts[0].requestId);
    next = () => new Response(JSON.stringify(data()));
    await poll();
    expect(
      target.querySelector('[aria-label="Session connection"]').textContent,
    ).toBe("Live");
  },
);
it("clears stale auth observations on a connection failure", async () => {
  await show();
  sessionStorage.setItem("grimoire-sign-in-reload", String(Date.now()));
  next = () => new Response("", { status: 401 });
  await poll();
  vi.setSystemTime(Date.now() + 2100);
  next = () => {
    throw new TypeError("offline");
  };
  await poll();
  next = () => new Response("", { status: 401 });
  await poll();
  expect(target.textContent).not.toContain("Signed out.");
  vi.setSystemTime(Date.now() + 2100);
  await poll();
  expect(target.textContent).toContain("Signed out.");
});
it("leaves validation errors visible and the disabled POST path unchanged", async () => {
  await show(false);
  next = () =>
    new Response(JSON.stringify({ error: "Invalid action" }), { status: 400 });
  send();
  await settle();
  expect(target.textContent).toContain("Invalid action");
  next = () => new Response("proxy", { status: 503 });
  send();
  await settle();
  expect(target.querySelector('[role="alert"]')).not.toBeNull();
});
