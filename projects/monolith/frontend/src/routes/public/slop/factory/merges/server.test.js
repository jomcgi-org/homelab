import { beforeEach, describe, it, expect, vi } from "vitest";
import { GET } from "./+server.js";
import { readPublicSnapshot } from "../../../../../lib/server/public-snapshots.js";
vi.mock("../../../../../lib/server/public-snapshots.js", () => ({
  readPublicSnapshot: vi.fn(),
}));
beforeEach(() => vi.clearAllMocks());

describe("merges public snapshot proxy", () => {
  it("serves the fixed public projection with its remaining lease", async () => {
    const headers = {
      "cache-control": "public, max-age=13, s-maxage=13, must-revalidate",
      etag: '\"testbuild-data\"',
    };
    readPublicSnapshot.mockResolvedValue({ body: '{"safe":true}', headers });
    const setHeaders = vi.fn();
    const response = await GET({ setHeaders });
    expect(readPublicSnapshot).toHaveBeenCalledWith("merges");
    expect(setHeaders).toHaveBeenCalledWith(headers);
    expect(await response.json()).toEqual({ safe: true });
  });
  it("makes a failed refresh uncacheable", async () => {
    readPublicSnapshot.mockRejectedValue(new Error("backend unavailable"));
    const setHeaders = vi.fn();
    await expect(GET({ setHeaders })).rejects.toMatchObject({ status: 503 });
    expect(setHeaders).toHaveBeenCalledWith({
      "cache-control": "no-store",
      "cloudflare-cdn-cache-control": "no-store",
    });
  });
});
