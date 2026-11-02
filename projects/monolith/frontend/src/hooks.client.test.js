// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { handleError } from "./hooks.client.js";
import shell from "./app.html?raw";
import { goto, preloadCode } from "$app/navigation";
import { page } from "$app/state";

vi.mock("$app/navigation", () => ({ goto: vi.fn(), preloadCode: vi.fn() }));

// The recovery script runs inline in app.html before SvelteKit starts.
const script = [...shell.matchAll(/<script>([\s\S]*?)<\/script>/g)]
  .map((match) => match[1])
  .find((body) => body.includes("__freshReload"));

const chunkError = new TypeError(
  "Failed to fetch dynamically imported module: https://jomcgi.dev/_app/immutable/nodes/5.CNMcGgaG.js",
);
let replace;

function boot(href = "/blog/post") {
  history.replaceState(null, "", href);
  new Function(script)();
}

beforeEach(() => {
  vi.resetAllMocks();
  sessionStorage.clear();
  replace = vi.spyOn(location, "replace").mockImplementation(() => {});
});
afterEach(() => {
  vi.restoreAllMocks();
  delete window.__freshReload;
  delete window.__grimoireRecovery;
  vi.useRealTimers();
});

test("a missing chunk reloads once past the edge cache", () => {
  boot();
  expect(window.__freshReload(chunkError)).toBe(true);
  const url = new URL(replace.mock.calls[0][0]);
  expect(url.pathname).toBe("/blog/post");
  expect(url.searchParams.get("_fresh")).toBeTruthy();
  // A second failure inside the guard window does not loop.
  expect(window.__freshReload(chunkError)).toBe(false);
  expect(replace).toHaveBeenCalledTimes(1);
});

test("Safari and Firefox chunk messages also reload", () => {
  boot();
  const safari = new TypeError("Importing a module script failed.");
  expect(window.__freshReload(safari)).toBe(true);
  sessionStorage.clear();
  const firefox = new TypeError(
    "error loading dynamically imported module: https://x/_app/a.js",
  );
  expect(window.__freshReload(firefox)).toBe(true);
});

test("other errors and blocked storage never reload", () => {
  boot();
  expect(window.__freshReload(new Error("boom"))).toBe(false);
  vi.spyOn(window, "sessionStorage", "get").mockImplementation(() => {
    throw new Error("blocked");
  });
  expect(window.__freshReload(chunkError)).toBe(false);
  expect(replace).not.toHaveBeenCalled();
});

test("the reload marker is removed from the address bar", () => {
  boot("/blog/post?tab=a&_fresh=abc#s2");
  expect(location.search).toBe("?tab=a");
  expect(location.hash).toBe("#s2");
});

test("handleError reloads for a missing chunk and keeps the default otherwise", () => {
  boot();
  expect(handleError({ error: chunkError, message: "Internal Error" })).toEqual(
    { message: "Loading the latest version" },
  );
  const log = vi.spyOn(console, "error").mockImplementation(() => {});
  const other = new Error("boom");
  expect(handleError({ error: other, message: "Internal Error" })).toEqual({
    message: "Internal Error",
  });
  expect(log).toHaveBeenCalledWith(other);
});

test("a failed client navigation reloads the page being opened", () => {
  boot();
  const event = { url: new URL("/blog/other?x=1", location.href) };
  handleError({ error: chunkError, event, message: "Internal Error" });
  const url = new URL(replace.mock.calls[0][0]);
  expect(url.pathname).toBe("/blog/other");
  expect(url.searchParams.get("x")).toBe("1");
  expect(url.searchParams.get("_fresh")).toBeTruthy();
});

const settle = async () => {
  for (let i = 0; i < 15; i++) await Promise.resolve();
};
test("staged recovery tries supported route loading before falling back", async () => {
  boot("/grimoire/campaigns/a/session");
  const save = vi.fn(() => true);
  window.__grimoireRecovery = { save };
  preloadCode.mockRejectedValueOnce(new Error("old build"));
  const url = new URL("/grimoire/campaigns/b/session", location.href);
  handleError({ error: chunkError, event: { url }, message: "Error" });
  expect(save).toHaveBeenCalledTimes(1);
  expect(replace).not.toHaveBeenCalled();
  await settle();
  expect(preloadCode).toHaveBeenCalledWith(url.pathname);
  expect(replace).toHaveBeenCalledTimes(1);
  expect(save).toHaveBeenCalledTimes(2);
});
test("a recovered navigation keeps the page in place and still guards retries", async () => {
  boot("/grimoire/campaigns/a/session");
  window.__grimoireRecovery = { save: vi.fn(() => true) };
  const url = new URL("/grimoire/campaigns/b/session", location.href);
  preloadCode.mockResolvedValueOnce();
  goto.mockImplementationOnce(async () => {
    page.url = url;
    page.status = 200;
  });
  handleError({ error: chunkError, event: { url }, message: "Error" });
  await settle();
  expect(goto).toHaveBeenCalledWith(url, { keepFocus: true, noScroll: true });
  expect(replace).not.toHaveBeenCalled();
  expect(window.__freshReload(chunkError)).toBe(false);
});
test("a stalled preload is bounded and repeated errors do not start a second recovery", async () => {
  vi.useFakeTimers();
  boot("/grimoire/campaigns/a/session");
  window.__grimoireRecovery = { save: vi.fn(() => true) };
  preloadCode.mockReturnValueOnce(new Promise(() => {}));
  const url = new URL("/grimoire/campaigns/b/session", location.href);
  handleError({ error: chunkError, event: { url }, message: "Error" });
  handleError({ error: chunkError, event: { url }, message: "Error" });
  await vi.advanceTimersByTimeAsync(1501);
  expect(preloadCode).toHaveBeenCalledTimes(1);
  expect(replace).toHaveBeenCalledTimes(1);
});
test("failed saves and leaving the session never replace the page", async () => {
  boot("/grimoire/campaigns/a/session");
  window.__grimoireRecovery = { save: () => false };
  expect(window.__freshReload(chunkError)).toBe(false);
  expect(replace).not.toHaveBeenCalled();
  sessionStorage.clear();
  window.__grimoireRecovery = { save: () => true };
  expect(window.__freshReload(chunkError)).toBe(true);
  delete window.__grimoireRecovery;
  history.replaceState(null, "", "/other-navigation");
  await settle();
  expect(replace).not.toHaveBeenCalled();
});

test("an error-page unmount still uses the saved snapshot for fallback", async () => {
  boot("/grimoire/campaigns/a/session");
  const save = vi.fn(() => true);
  window.__grimoireRecovery = { save };
  expect(window.__freshReload(chunkError)).toBe(true);
  delete window.__grimoireRecovery;
  await settle();
  expect(save).toHaveBeenCalledTimes(1);
  expect(replace).toHaveBeenCalledTimes(1);
});
