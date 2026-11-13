import { act, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionEventView } from "../api/schema";
import { ids } from "../test/fixtures";
import { event } from "../test/fixtures/session";
import { voices } from "../test/fixtures/turns";
import { renderPanel } from "../turns/renderPanel";
import { ReadAloud, type ReadAloudProps } from "./ReadAloud";

/** A stand-in for the browser's speech queue: records what was asked of it. */
class Utterance {
  rate = 1;
  pitch = 1;
  lang = "";
  onend: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(readonly text: string) {}
}
let spoken: Utterance[];
let cancel: ReturnType<typeof vi.fn>;

function installSpeech() {
  spoken = [];
  cancel = vi.fn();
  vi.stubGlobal("SpeechSynthesisUtterance", Utterance);
  vi.stubGlobal("speechSynthesis", {
    getVoices: () => [],
    speak: (utterance: Utterance) => spoken.push(utterance),
    cancel,
  });
}

const STORED = `grimoire:read-aloud:${ids.owned}`;
const narration = (text: string, overrides: Partial<SessionEventView> = {}) =>
  event({ kind: "narration", body: { text }, ...overrides });
const history = [narration("Rain hammers the shutters.")];

function open(props: Partial<ReadAloudProps> = {}) {
  const panel = (events: SessionEventView[]) => (
    <ReadAloud
      campaignId={ids.owned}
      role="dm"
      characterId={null}
      events={events}
      voices={voices}
      {...props}
    />
  );
  const view = renderPanel(panel(props.events ?? history), {
    "POST /campaigns/{campaign_id}/tts": { status: 404 },
  });
  return {
    ...view,
    receive: (events: SessionEventView[]) =>
      act(async () => {
        view.rerender(panel(events));
      }),
  };
}
const box = (name: string) =>
  screen.getByRole("checkbox", { name }) as HTMLInputElement;

beforeEach(() => window.localStorage.clear());
afterEach(() => vi.unstubAllGlobals());

describe("ReadAloud", async () => {
  it("hides its controls where the browser cannot speak", async () => {
    open();
    expect(screen.queryByRole("region", { name: "Read aloud" })).toBeNull();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("reads narration by default for the DM and not for a player", async () => {
    installSpeech();
    open();
    expect(box("Read DM narration").checked).toBe(true);
    expect(
      screen.queryByRole("checkbox", {
        name: "Read my reveals and private narration",
      }),
    ).toBeNull();
  });

  it("offers a player both choices, off until chosen", async () => {
    installSpeech();
    open({ role: "player", characterId: ids.mira });
    expect(box("Read DM narration").checked).toBe(false);
    expect(box("Read my reveals and private narration").checked).toBe(false);
  });

  it("stays silent for the history already on screen", async () => {
    installSpeech();
    open();
    expect(spoken).toHaveLength(0);
  });

  it("speaks narration that arrives later, with the speaker's saved voice", async () => {
    installSpeech();
    const { receive } = open();
    await receive([
      ...history,
      narration("The cellar door creaks.", {
        body: { text: "The cellar door creaks.", speaker_key: "Innkeeper" },
      }),
    ]);
    expect(spoken.map((utterance) => utterance.text)).toEqual([
      "The cellar door creaks.",
    ]);
    expect(spoken[0]!.rate).toBe(1.2);
  });

  it("falls back to the narrator's voice for an unknown speaker", async () => {
    installSpeech();
    const { receive } = open();
    await receive([...history, narration("A bell tolls.")]);
    expect(spoken[0]!.rate).toBe(0.9);
    expect(spoken[0]!.lang).toBe("en-GB");
  });

  it("does not speak when narration is turned off, and remembers the choice", async () => {
    installSpeech();
    const { user, receive } = open();
    await user.click(box("Read DM narration"));
    expect(cancel).toHaveBeenCalled();
    await receive([...history, narration("A bell tolls.")]);
    expect(spoken).toHaveLength(0);
    expect(JSON.parse(window.localStorage.getItem(STORED)!)).toEqual({
      narration: false,
      reveals: false,
    });
  });

  it("restores the choices saved on this device for this campaign", async () => {
    installSpeech();
    window.localStorage.setItem(
      STORED,
      JSON.stringify({ narration: true, reveals: true }),
    );
    open({ role: "player", characterId: ids.mira });
    expect(box("Read DM narration").checked).toBe(true);
    expect(box("Read my reveals and private narration").checked).toBe(true);
  });

  it("reads a player's private narration only when they opt in", async () => {
    installSpeech();
    const whisper = narration("Only you hear the whisper.", {
      audience: "pcs",
      audience_pc_ids: [ids.mira],
    });
    const { user, receive } = open({ role: "player", characterId: ids.mira });
    await receive([...history, whisper]);
    expect(spoken).toHaveLength(0);
    await user.click(box("Read my reveals and private narration"));
    await receive([
      ...history,
      whisper,
      narration("And again.", { audience: "pcs", audience_pc_ids: [ids.mira] }),
    ]);
    expect(spoken.map((utterance) => utterance.text)).toEqual(["And again."]);
  });

  it("never speaks a retracted event", async () => {
    installSpeech();
    const { receive } = open();
    await receive([
      ...history,
      narration("Struck from the record.", {
        retracted_at: "2026-11-10T19:30:00Z",
      }),
    ]);
    expect(spoken).toHaveLength(0);
  });

  it("stops speech on request, and only while something is playing", async () => {
    installSpeech();
    const { user, receive } = open();
    const stop = () =>
      screen.getByRole("button", {
        name: "Stop read-aloud",
      }) as HTMLButtonElement;
    const idle = () =>
      stop().disabled || stop().getAttribute("aria-disabled") === "true";
    expect(idle()).toBe(true);
    await receive([...history, narration("A bell tolls.")]);
    expect(idle()).toBe(false);
    cancel.mockClear();
    await user.click(stop());
    expect(cancel).toHaveBeenCalledOnce();
    expect(idle()).toBe(true);
  });

  it("stops speaking when the session screen lets go of it", async () => {
    installSpeech();
    const { receive, rerender } = open();
    await receive([...history, narration("A bell tolls.")]);
    cancel.mockClear();
    act(() => rerender(null));
    expect(cancel).toHaveBeenCalled();
  });
});
