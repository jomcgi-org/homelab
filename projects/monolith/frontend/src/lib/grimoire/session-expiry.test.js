import { afterEach, describe, expect, it, vi } from "vitest";
import {
  claimReload,
  expiryCheck,
  keepDraft,
  signInExpired,
  takeDraft,
} from "./session-expiry.js";

const campaignId = "11111111-1111-4111-8111-111111111111";
const pcA = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const npc = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const dmViewer = { id: "user-dm", role: "dm" };

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

  it("confirms expiry only with a second signal two seconds after the first", () => {
    const expiry = expiryCheck();
    expect(expiry.signal(10_000)).toBe(false);
    expect(expiry.signal(11_999)).toBe(false);
    expect(expiry.signal(12_000)).toBe(true);
    expect(expiry.signal(14_000)).toBe(true);
    // An answer in between starts the count again.
    expiry.reset();
    expect(expiry.signal(16_000)).toBe(false);
    // A clock that went back restarts the wait instead of confirming early.
    expect(expiry.signal(5_000)).toBe(false);
    expect(expiry.signal(7_000)).toBe(true);
  });

  it("does not let a stale lone signal confirm a later one", () => {
    const expiry = expiryCheck();
    // A hidden tab stops polling, so nothing answered the first signal.
    expect(expiry.signal(10_000)).toBe(false);
    expect(expiry.signal(10_000 + 31_000)).toBe(false);
    expect(expiry.signal(10_000 + 33_000)).toBe(true);
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
    expect(keepDraft(campaignId, dmViewer, { text: "Hold the door" })).toBe(
      false,
    );
    expect(keepDraft(campaignId, dmViewer, { text: "  " })).toBe(true);
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
      pending: { signature: "sig", id: "request-1" },
    };
    expect(keepDraft(campaignId, dmViewer, composer)).toBe(true);
    const page = {
      dm: true,
      characters: [{ id: pcA }],
      speakers: ["narrator", npc, "label:custom"],
    };
    expect(takeDraft(campaignId, dmViewer, page)).toEqual(composer);
    expect(takeDraft(campaignId, dmViewer, page)).toBe(null);
  });

  it("restores a free-label speaker the page still offers", () => {
    vi.stubGlobal("sessionStorage", memoryStorage());
    keepDraft(campaignId, dmViewer, {
      text: "Halt, who goes there?",
      audience: "table",
      speakerKey: "label:custom",
      speakerLabel: "Captain North",
    });
    expect(
      takeDraft(campaignId, dmViewer, {
        dm: true,
        characters: [],
        speakers: ["narrator", "label:custom"],
      }),
    ).toMatchObject({
      speakerKey: "label:custom",
      speakerLabel: "Captain North",
    });
  });

  it("drops a draft another viewer kept in this tab", () => {
    vi.stubGlobal("sessionStorage", memoryStorage());
    const composer = { text: "The mayor is the cultist.", audience: "dm" };
    const page = { dm: false, characters: [{ id: pcA }] };
    for (const viewer of [
      { id: "user-player", role: "player" },
      { id: "user-dm", role: "player" },
      { role: "dm" },
    ]) {
      keepDraft(campaignId, dmViewer, composer);
      expect(takeDraft(campaignId, viewer, page)).toBe(null);
      // The mismatch also clears it, so the DM gets nothing back later.
      expect(takeDraft(campaignId, dmViewer, page)).toBe(null);
    }
  });

  it("falls back to the DM and the narrator for choices the page lost", () => {
    vi.stubGlobal("sessionStorage", memoryStorage());
    keepDraft(campaignId, dmViewer, {
      text: "Secret plan",
      audience: `pc:${pcA}`,
      replyTo: { id: "event-1", name: "Aria" },
      speakerKey: npc,
    });
    expect(
      takeDraft(campaignId, dmViewer, { dm: true, characters: [] }),
    ).toMatchObject({
      text: "Secret plan",
      audience: "dm",
      replyTo: null,
      speakerKey: "narrator",
    });
  });
});
