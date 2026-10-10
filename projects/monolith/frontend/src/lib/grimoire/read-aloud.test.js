import { describe, expect, it, vi } from "vitest";
import {
  createEventReader,
  createSpeaker,
  loadReadAloudSettings,
  readAloudDefaults,
  resolveVoice,
  saveReadAloudSettings,
  voicePreset,
} from "./read-aloud.js";

const narration = (id, seq, extra = {}) => ({
  id,
  seq,
  kind: "narration",
  audience: "table",
  body: { text: id },
  ...extra,
});
const reveal = (id, seq, extra = {}) => ({
  id,
  seq,
  kind: "reveal",
  audience: "pcs",
  audience_pc_ids: ["pc-a"],
  body: {
    reveals: [
      {
        entity_id: "npc",
        name: "Mara",
        entity: { name: "Mara", revealed_details: { clue: "Silver compass" } },
      },
    ],
  },
  ...extra,
});

function device(voices = []) {
  const listeners = new Map();
  let current;
  const synth = {
    getVoices: vi.fn(() => voices),
    speak: vi.fn((utterance) => {
      expect(current).toBeUndefined();
      current = utterance;
    }),
    cancel: vi.fn(() => {
      current = undefined;
    }),
    addEventListener: vi.fn((name, fn) => listeners.set(name, fn)),
    removeEventListener: vi.fn((name) => listeners.delete(name)),
  };
  const micPauser = { pause: vi.fn(), resume: vi.fn() };
  const speaker = createSpeaker({
    synth,
    Utterance: class {
      constructor(text) {
        this.text = text;
      }
    },
    micPauser,
  });
  return {
    synth,
    speaker,
    micPauser,
    listeners,
    finish(error = false) {
      const utterance = current;
      current = undefined;
      utterance[error ? "onerror" : "onend"]();
    },
  };
}
const items = (...events) =>
  events.map((event) => ({ event, text: event.body.text || event.id }));

describe("read-aloud queue", () => {
  it("speaks a delivery in seq order with no overlap and advances after errors", () => {
    const d = device();
    d.speaker.deliver(
      items(
        narration("third", 3),
        narration("first", 1),
        narration("second", 2),
      ),
    );
    expect(d.synth.speak.mock.calls.map(([u]) => u.text)).toEqual(["first"]);
    d.finish();
    d.finish(true);
    d.finish();
    expect(d.synth.speak.mock.calls.map(([u]) => u.text)).toEqual([
      "first",
      "second",
      "third",
    ]);
    expect(d.micPauser.pause).toHaveBeenCalledTimes(1);
    expect(d.micPauser.resume).toHaveBeenCalledTimes(1);
  });
  it("new narration cancels earlier current and queued speech, ignoring stale callbacks", () => {
    const d = device();
    d.speaker.deliver(items(reveal("old-current", 1), reveal("old-queued", 2)));
    const old = d.synth.speak.mock.calls[0][0];
    d.speaker.deliver(
      items(narration("new-first", 3), narration("new-second", 4)),
    );
    old.onend();
    old.onerror();
    expect(d.synth.cancel).toHaveBeenCalledTimes(1);
    expect(d.micPauser.resume).toHaveBeenCalledTimes(1);
    d.finish();
    d.finish();
    expect(d.synth.speak.mock.calls.map(([u]) => u.text)).toEqual([
      "old-current",
      "new-first",
      "new-second",
    ]);
    expect(d.micPauser.pause).toHaveBeenCalledTimes(2);
    expect(d.micPauser.resume).toHaveBeenCalledTimes(2);
  });
  it("stop clears the queue and resumes the mic, dispose removes the voice listener", () => {
    const d = device();
    d.speaker.deliver(items(reveal("current", 1), reveal("queued", 2)));
    const old = d.synth.speak.mock.calls[0][0];
    d.speaker.stop();
    old.onend();
    expect(d.synth.speak).toHaveBeenCalledTimes(1);
    expect(d.synth.cancel).toHaveBeenCalledTimes(1);
    expect(d.micPauser.resume).toHaveBeenCalledTimes(1);
    d.speaker.dispose();
    expect(d.listeners.size).toBe(0);
    expect(d.micPauser.resume).toHaveBeenCalledTimes(1);
  });
  it("unavailable speech is a no-op", () => {
    const micPauser = { pause: vi.fn(), resume: vi.fn() };
    const speaker = createSpeaker({ synth: null, Utterance: null, micPauser });
    speaker.speak(narration("hello", 1));
    speaker.stop();
    speaker.dispose();
    expect(speaker.available).toBe(false);
    expect(micPauser.pause).not.toHaveBeenCalled();
  });
});

describe("device voices", () => {
  const voices = [
    { name: "Default", lang: "de-DE", default: true },
    { name: "English North", lang: "en-US" },
    { name: "French", lang: "fr-FR" },
  ];
  it("resolves name, exact lang, subtag, device default and empty voice lists", () => {
    expect(
      resolveVoice(voices, { names: ["missing", "NORTH"], lang: "fr-FR" }),
    ).toBe(voices[1]);
    expect(resolveVoice(voices, { lang: "FR-fr" })).toBe(voices[2]);
    expect(resolveVoice(voices, { lang: "en-GB" })).toBe(voices[1]);
    expect(resolveVoice(voices, { lang: "it-IT" })).toBe(voices[0]);
    expect(resolveVoice([], { lang: "en" })).toBeUndefined();
    expect(resolveVoice(voices.slice(1), {})).toBeUndefined();
  });
  it("uses received ref keys, narrator fallback, rate and pitch, and asynchronous voices", () => {
    const d = device();
    const presets = [
      {
        speaker_key: "ref:0123456789abcdef0123",
        voice_hint: { names: ["north"] },
        rate: 0.8,
        pitch: 0.6,
      },
      { speaker_key: "narrator", rate: 1.2, pitch: 1.1 },
    ];
    expect(voicePreset(presets, "missing")).toBe(presets[1]);
    expect(voicePreset(presets, undefined)).toBe(presets[1]);
    expect(voicePreset([], "missing")).toMatchObject({ rate: 1, pitch: 1 });
    const event = narration("hello", 1, {
      body: { text: "hello", speaker_key: presets[0].speaker_key },
    });
    d.speaker.speak(event, presets);
    expect(d.synth.speak.mock.calls[0][0]).not.toHaveProperty("voice");
    d.finish();
    d.synth.getVoices.mockReturnValue(voices);
    d.listeners.get("voiceschanged")();
    d.speaker.speak(event, presets);
    expect(d.synth.speak.mock.calls[1][0]).toMatchObject({
      voice: voices[1],
      rate: 0.8,
      pitch: 0.6,
    });
  });
});

describe("received event selection", () => {
  it("speaks a PC-scoped reveal only on its recipient device, never the other player or room DM", () => {
    const serverEvents = [narration("public", 1), reveal("secret", 2)];
    for (const [role, pcId, expected] of [
      ["player", "pc-a", 1],
      ["player", "pc-b", 0],
      ["dm", null, 0],
    ]) {
      // Mirrors the backend audience predicate before delivering a feed to a device.
      const received = serverEvents.filter(
        (event) =>
          role === "dm" ||
          event.audience === "table" ||
          (event.audience === "pcs" && event.audience_pc_ids.includes(pcId)),
      );
      const reader = createEventReader();
      const d = device();
      const settings = { role, pcId, narration: false, reveals: true };
      reader.select([], settings);
      const selected = reader.select(received, settings);
      d.speaker.deliver(selected);
      expect(selected).toHaveLength(expected);
      expect(d.synth.speak).toHaveBeenCalledTimes(expected);
      if (expected)
        expect(d.synth.speak.mock.calls[0][0].text).toBe(
          "Mara. clue: Silver compass",
        );
    }
  });
  it("marks backlog and disabled events handled, skips retractions, and keeps DM private events silent", () => {
    const reader = createEventReader();
    const settings = { role: "dm", narration: true, reveals: true };
    expect(reader.select([narration("backlog", 1)], settings)).toEqual([]);
    const fresh = [
      narration("private", 2, { audience: "dm" }),
      narration("pc", 3, { audience: "pcs", audience_pc_ids: ["pc-a"] }),
      reveal("reveal", 4),
      narration("retracted", 5, { retracted_at: "now" }),
      narration("public", 6),
    ];
    expect(reader.select(fresh, settings).map(({ event }) => event.id)).toEqual(
      ["public"],
    );
    expect(reader.select(fresh, settings)).toEqual([]);
    expect(
      reader.select([narration("off", 7)], { ...settings, narration: false }),
    ).toEqual([]);
    expect(reader.select([narration("off", 7)], settings)).toEqual([]);
  });
  it("player toggles select table narration and own private replies independently", () => {
    for (const [narrationOn, reveals, expected] of [
      [false, false, []],
      [true, false, ["table"]],
      [false, true, ["mine"]],
      [true, true, ["table", "mine"]],
    ]) {
      const reader = createEventReader();
      const settings = {
        role: "player",
        pcId: "pc-a",
        narration: narrationOn,
        reveals,
      };
      reader.select([], settings);
      const feed = [
        narration("table", 1),
        narration("mine", 2, { audience: "pcs", audience_pc_ids: ["pc-a"] }),
        narration("other", 3, { audience: "pcs", audience_pc_ids: ["pc-b"] }),
        narration("dm", 4, { audience: "dm" }),
      ];
      expect(
        reader.select(feed, settings).map(({ event }) => event.id),
      ).toEqual(expected);
    }
  });
  it("honors silent and individually retracted reveal items and name-only projections", () => {
    const reader = createEventReader();
    const settings = { role: "player", reveals: true };
    reader.select([], settings);
    const event = reveal("batch", 1, {
      body: {
        retracted_entity_ids: ["gone"],
        reveals: [
          { entity_id: "silent", name: "SILENT", silent: true },
          { entity_id: "gone", name: "GONE" },
          { entity_id: "retracted", name: "RETRACTED", retracted: true },
          {
            entity_id: "name",
            name: "Known",
            grant_scope: "name_only",
            entity: { name: "Known", description: "HIDDEN" },
          },
        ],
      },
    });
    expect(reader.select([event], settings).map(({ text }) => text)).toEqual([
      "Known",
    ]);
  });
  it("defaults per role and persists per campaign, surviving unavailable storage", () => {
    expect(readAloudDefaults("dm")).toEqual({
      narration: true,
      reveals: false,
    });
    expect(readAloudDefaults("player")).toEqual({
      narration: false,
      reveals: false,
    });
    const data = new Map();
    const storage = {
      getItem: (key) => data.get(key),
      setItem: (key, value) => data.set(key, value),
    };
    saveReadAloudSettings(storage, "one", { narration: true, reveals: true });
    expect(loadReadAloudSettings(storage, "one", "player")).toEqual({
      narration: true,
      reveals: true,
    });
    expect(loadReadAloudSettings(storage, "two", "player")).toEqual(
      readAloudDefaults("player"),
    );
    expect(
      loadReadAloudSettings(
        {
          getItem() {
            throw new Error();
          },
        },
        "one",
        "dm",
      ),
    ).toEqual(readAloudDefaults("dm"));
    expect(() =>
      saveReadAloudSettings(
        {
          setItem() {
            throw new Error();
          },
        },
        "one",
        {},
      ),
    ).not.toThrow();
  });
});
