// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Session from "../../routes/friends/grimoire/campaigns/[id]/session/+page.svelte";
import shell from "../../app.html?raw";
import { handleError } from "../../hooks.client.js";
import { RECOVERY_KEY } from "./rollout-recovery.js";

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
  for (let i = 0; i < 16; i++) {
    await tick();
    await Promise.resolve();
  }
};
async function poll() {
  document.dispatchEvent(new Event("visibilitychange"));
  await settle();
}
async function show(enabled = true, overrides = {}) {
  component = mount(Session, {
    target,
    props: {
      data: { ...data(), rolloutRecoveryEnabled: enabled, ...overrides },
    },
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
  history.replaceState(null, "", "/grimoire/campaigns/campaign/session");
  new Function(
    [...shell.matchAll(/<script>([\s\S]*?)<\/script>/g)]
      .map((match) => match[1])
      .find((script) => script.includes("__freshReload")),
  )();
  vi.spyOn(location, "replace").mockImplementation(() => {});
  vi.spyOn(console, "error").mockImplementation(() => {});
  next = () => new Response(JSON.stringify(data()));
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url, options) => {
      if (options?.method === "POST") {
        posts.push(JSON.parse(options.body));
        return next();
      }
      if (String(url).includes("?notes=")) return new Response("[]");
      if (String(url).includes("?inventory=")) return new Response("[]");
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
  delete window.__freshReload;
  delete window.__grimoireRecovery;
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

function input(label, value) {
  const field = [...target.querySelectorAll("input,textarea,select")].find(
    (el) =>
      el.getAttribute("aria-label") === label ||
      el.labels?.[0]?.textContent.trim() === label,
  );
  expect(field, label).toBeTruthy();
  field.value = value;
  field.dispatchEvent(
    new Event(field.tagName === "SELECT" ? "change" : "input", {
      bubbles: true,
    }),
  );
}
function click(text) {
  const button = [...target.querySelectorAll("button")].find(
    (el) => el.textContent.trim() === text,
  );
  expect(button, text).toBeTruthy();
  button.click();
}
const chunkError = new TypeError(
  "Failed to fetch dynamically imported module: /_app/old.js",
);
async function chunkReload() {
  handleError({
    error: chunkError,
    event: { url: new URL(location.href) },
    message: "Internal Error",
  });
  await settle();
}
it("saves through the real chunk callback and restores composer/request ID, tab and dirty panels after initialization", async () => {
  await show();
  next = () => {
    throw new TypeError("lost response");
  };
  send();
  await settle();
  const requestId = posts[0].requestId;
  next = () => new Response(JSON.stringify(data()));
  click("Party notes");
  await settle();
  input("Note title", "Remember the door");
  input("Note text", "Do not open it");
  input("Handout title", "A map");
  input("Handout text", "Secret trail");
  click("Add row");
  await settle();
  input("Name", "Hidden ogre");
  input("Initiative", "17");
  input("Language hint", "fr-CA");
  input("Preferred voice names", "Alice");
  input("Roll label", "Find the trail");
  click("Inventory");
  await settle();
  input("Item name", "Compass");
  await chunkReload();
  expect(location.replace).toHaveBeenCalledTimes(1);
  expect(posts).toHaveLength(1);
  expect(JSON.parse(sessionStorage.getItem(RECOVERY_KEY)).version).toBe(1);
  await unmount(component);
  component = null;
  await show();
  expect(target.querySelector("#message").value).toBe("Hold the door");
  expect(
    [...target.querySelectorAll("button")]
      .find((el) => el.textContent === "Inventory")
      .getAttribute("aria-pressed"),
  ).toBe("true");
  const value = (label) =>
    [...target.querySelectorAll("input,textarea")].find(
      (el) =>
        el.getAttribute("aria-label") === label ||
        el.labels?.[0]?.textContent.trim() === label,
    )?.value;
  expect(value("Note title")).toBe("Remember the door");
  expect(value("Note text")).toBe("Do not open it");
  expect(value("Handout title")).toBe("A map");
  expect(value("Handout text")).toBe("Secret trail");
  expect(value("Name")).toBe("Hidden ogre");
  expect(value("Initiative")).toBe("17");
  expect(value("Language hint")).toBe("fr-CA");
  expect(value("Preferred voice names")).toBe("Alice");
  expect(value("Item name")).toBe("Compass");
  expect(value("Roll label")).toBe("Find the trail");
  expect(posts).toHaveLength(1);
  send();
  await settle();
  expect(posts[1].requestId).toBe(requestId);
});

it.each([
  "viewer",
  "role",
  "session",
  "ended",
  "permissions",
  "corrupt",
  "expired",
])("rejects %s snapshots and the paired composer", async (change) => {
  await show();
  send();
  await settle();
  // Successful sends clear the composer. Keep an unsent draft for the reload.
  input("Set the scene", "Private draft");
  await chunkReload();
  await unmount(component);
  component = null;
  const overrides = {};
  if (change === "viewer") overrides.user = { id: "other" };
  if (change === "role")
    overrides.campaign = { ...data().campaign, role: "player" };
  if (change === "permissions")
    overrides.campaign = { ...data().campaign, permissions: { write: false } };
  if (change === "session")
    overrides.session = { id: "other", status: "active" };
  if (change === "ended")
    overrides.session = { ...data().session, status: "ended" };
  if (change === "corrupt") sessionStorage.setItem(RECOVERY_KEY, "{broken");
  if (change === "expired") vi.setSystemTime(Date.now() + 16 * 60_000);
  await show(true, overrides);
  expect(target.querySelector("#message")?.value || "").toBe("");
  expect(posts).toHaveLength(1);
});
it("does not reload on failed snapshot storage or loop after a guarded reload", async () => {
  await show();
  input("Set the scene", "Unstored draft");
  const store = sessionStorage;
  const blocked = vi
    .spyOn(window, "sessionStorage", "get")
    .mockImplementation(() => ({
      getItem: (key) => store.getItem(key),
      removeItem: (key) => store.removeItem(key),
      setItem: (key, value) => {
        if (key !== "fresh-reload") throw new Error("quota");
        store.setItem(key, value);
      },
    }));
  await chunkReload();
  expect(location.replace).not.toHaveBeenCalled();
  expect(target.querySelector("#message").value).toBe("Unstored draft");
  blocked.mockRestore();
  vi.setSystemTime(Date.now() + 31_000);
  await chunkReload();
  await chunkReload();
  expect(location.replace).toHaveBeenCalledTimes(1);
});
it("keeps chunk recovery disabled when the server flag is false", async () => {
  await show(false);
  expect(window.__grimoireRecovery).toBeUndefined();
  await chunkReload();
  expect(sessionStorage.getItem(RECOVERY_KEY)).toBeNull();
  expect(location.replace).toHaveBeenCalledTimes(1);
});
