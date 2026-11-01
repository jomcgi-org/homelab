import { afterEach, describe, expect, it, vi } from "vitest";
import {
  claimReload,
  keepDraft,
  signInExpired,
  takeDraft,
} from "./session-expiry.js";

const campaignId = "11111111-1111-4111-8111-111111111111";
const pcA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const npc = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";

function memoryStorage() {
  const rows = new Map();
  return {
    getItem: (key) => rows.get(key) ?? null,
    setItem: (key, value) => rows.set(key, String(value)),
    removeItem: (key) => rows.delete(key),
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("session sign-in expiry", () => {
  it("treats a 401 and a manual redirect as expiry, nothing else", () => {
    expect(signInExpired({ type: "basic", status: 401 })).toBe(true);
    expect(signInExpired({ type: "opaqueredirect", status: 0 })).toBe(true);
    for (const status of [200, 400, 403, 502])
      expect(signInExpired({ type: "basic", status })).toBe(false);
  });

  it("allows one reload a minute and none without storage", () => {
    vi.stubGlobal("sessionStorage", memoryStorage());
    expect(claimReload(1_000_000)).toBe(true);
    expect(claimReload(1_059_999)).toBe(false);
    expect(claimReload(1_060_000)).toBe(true);

    vi.stubGlobal("sessionStorage", {
      getItem: () => {
        throw new Error("blocked");
      },
    });
    expect(claimReload(2_000_000)).toBe(false);
    vi.stubGlobal("sessionStorage", undefined);
    expect(claimReload(2_000_000)).toBe(false);
  });

  it("reports a draft it could not keep", () => {
    vi.stubGlobal("sessionStorage", {
      setItem: () => {
        throw new Error("quota");
      },
      removeItem: () => {
        throw new Error("blocked");
      },
    });
    expect(keepDraft(campaignId, { text: "Hold the door" })).toBe(false);
    expect(keepDraft(campaignId, { text: "  " })).toBe(true);
  });

  it("keeps a DM's composer and reads it back once", () => {
    vi.stubGlobal("sessionStorage", memoryStorage());
    const composer = {
      text: "You hear a whisper.",
      audience: `pc:${pcA}`,
      replyTo: { id: "event-1", name: "Aria" },
      resolved: false,
      speakerKey: npc,
      speakerLabel: "",
    };
    expect(keepDraft(campaignId, composer)).toBe(true);
    const page = {
      dm: true,
      characters: [{ id: pcA }],
      npcs: [{ id: npc }],
    };
    expect(takeDraft(campaignId, page)).toEqual(composer);
    expect(takeDraft(campaignId, page)).toBe(null);
  });

  it("falls back to the DM and the narrator for choices the page lost", () => {
    vi.stubGlobal("sessionStorage", memoryStorage());
    keepDraft(campaignId, {
      text: "Secret plan",
      audience: `pc:${pcA}`,
      replyTo: { id: "event-1", name: "Aria" },
      speakerKey: npc,
    });
    expect(takeDraft(campaignId, { dm: true, characters: [] })).toMatchObject({
      text: "Secret plan",
      audience: "dm",
      replyTo: null,
      speakerKey: "narrator",
    });
  });
});
