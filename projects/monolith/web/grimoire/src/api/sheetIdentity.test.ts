import { afterEach, expect, it, vi } from "vitest";
import { requestJson } from "./client";

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
