import { afterEach, expect, it, vi } from "vitest";
import { requestJson, requestViewerId } from "./client";

afterEach(() => vi.unstubAllGlobals());

it.each([null, "other-account"])("rejects a sheet response authorized for %s instead of the editor account", async (viewer) => {
  const headers = new Headers({ "content-type": "application/json" });
  if (viewer) headers.set("X-Grimoire-Viewer-Id", viewer);
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { headers })));
  await expect(requestJson("/api/grimoire/sheet", { expectedViewerId: "original-account" })).rejects.toMatchObject({ status: 403 });
});

it("accepts only a fresh successful response for the expected account", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { headers: {
    "content-type": "application/json", "X-Grimoire-Viewer-Id": "original-account",
  } })));
  await expect(requestJson("/api/grimoire/sheet", { expectedViewerId: "original-account" })).resolves.toEqual({});
});

it.each(["current-account", ""])("reads current sign-out identity %s without a cache", async (viewer) => {
  const fetch = vi.fn(async () => new Response(null, {
    status: 204, headers: { "X-Grimoire-Viewer-Id": viewer },
  }));
  vi.stubGlobal("fetch", fetch);
  await expect(requestViewerId("/api/grimoire/lobby/identity")).resolves.toBe(viewer || null);
  expect(fetch).toHaveBeenCalledWith("/api/grimoire/lobby/identity", expect.objectContaining({
    method: "HEAD", cache: "no-store", credentials: "same-origin", redirect: "manual",
  }));
});

it.each([200, 204, 403, 503])("refuses an unconfirmed sign-out identity (%s)", async (status) => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status })));
  await expect(requestViewerId("/api/grimoire/lobby/identity")).rejects.toBeInstanceOf(Error);
});
