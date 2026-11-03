import { afterEach, expect, it, vi } from "vitest";
import { load } from "./+page.server.js";
import { sessionState } from "$lib/server/grimoire-session.js";

vi.mock("$lib/server/grimoire-session.js", () => ({ sessionState: vi.fn() }));
afterEach(() => vi.unstubAllEnvs());

it.each([undefined, "false", "1", "TRUE", "true"])(
  "exposes only an exact server boolean for %s",
  async (flag) => {
    vi.stubEnv("GRIMOIRE_PLAY_ENABLED", "true");
    vi.stubEnv("GRIMOIRE_ROLLOUT_RECOVERY_ENABLED", flag);
    sessionState.mockResolvedValue({ campaign: { id: "campaign" } });
    const setHeaders = vi.fn();
    const result = await load({
      fetch: vi.fn(),
      cookies: {},
      params: { id: "campaign" },
      setHeaders,
      url: new URL("https://friends.test/grimoire?session=selected"),
    });
    expect(result).toEqual({
      campaign: { id: "campaign" },
      rolloutRecoveryEnabled: flag === "true",
    });
    expect(sessionState).toHaveBeenLastCalledWith(
      expect.any(Function),
      {},
      "campaign",
      "selected",
    );
    expect(setHeaders).toHaveBeenCalledWith({
      "cache-control": "private, no-store",
    });
  },
);
