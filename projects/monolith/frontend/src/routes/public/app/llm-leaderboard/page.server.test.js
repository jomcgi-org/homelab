import { describe, it, expect, vi } from "vitest";
import { load } from "./+page.server.js";
import { PAGE_CACHE_CONTROL } from "../../../../lib/cache-headers.js";

describe("/public/app/llm-leaderboard load", () => {
  it("uses the shared short page cache so a republish shows within a minute", () => {
    const setHeaders = vi.fn();

    const result = load({ setHeaders });

    expect(setHeaders).toHaveBeenCalledWith(
      expect.objectContaining({
        "cache-control": PAGE_CACHE_CONTROL,
        "cloudflare-cdn-cache-control":
          "public, max-age=60, stale-while-revalidate=86400, stale-if-error=31536000",
      }),
    );
    expect(result.leaderboard.models.length).toBeGreaterThan(0);
  });
});
