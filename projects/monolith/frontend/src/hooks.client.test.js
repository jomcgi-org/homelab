// @vitest-environment happy-dom
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { handleError } from "./hooks.client.js";
import shell from "./app.html?raw";

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
  sessionStorage.clear();
  replace = vi.spyOn(location, "replace").mockImplementation(() => {});
});
afterEach(() => {
  vi.restoreAllMocks();
  delete window.__freshReload;
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
