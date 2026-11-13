import { act, render, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ReadAloud } from "./ReadAloud";
import { installBackend } from "../test/backend";
import { Providers, createQueryClient } from "../app/Providers";
import { event } from "../test/fixtures/session";

let backend: ReturnType<typeof installBackend>;
afterEach(() => {
  backend?.restore();
  vi.unstubAllGlobals();
});

it("uses the API for fresh DM table events and cancels on campaign switch", async () => {
  const speak = vi.fn();
  const audioPlay = vi.fn(() => Promise.resolve());
  vi.stubGlobal("speechSynthesis", {
    getVoices: () => [],
    cancel: vi.fn(),
    speak,
  });
  vi.stubGlobal("SpeechSynthesisUtterance", class {});
  vi.stubGlobal(
    "Audio",
    class {
      play = audioPlay;
      pause() {}
      load() {}
      removeAttribute() {}
    },
  );
  let answer: (value: {
    clip_id: string;
    audio_base64: string;
  }) => void = () => {};
  backend = installBackend({
    "POST /campaigns/{campaign_id}/tts": () =>
      new Promise((resolve) => {
        answer = resolve;
      }),
    "GET /campaigns/{campaign_id}/tts/{event_id}/{clip_id}": {
      audio_base64: "UklGRg==",
    },
  });
  const history = event({
    id: "history",
    kind: "narration",
    audience: "table",
    seq: 1,
    body: { text: "Old text" },
  });
  const fresh = event({
    id: "fresh",
    kind: "narration",
    audience: "table",
    seq: 2,
    body: { text: "New text" },
  });
  const privateEvent = event({
    id: "private",
    kind: "narration",
    audience: "dm",
    seq: 3,
    body: { text: "DM secret" },
  });
  const view = (campaignId: string, events: (typeof history)[]) => (
    <Providers client={createQueryClient()}>
      <ReadAloud
        campaignId={campaignId}
        role="dm"
        characterId={null}
        events={events}
        voices={[]}
      />
    </Providers>
  );
  const { rerender, unmount } = render(view("first", [history]));
  expect(backend.callsTo("POST /campaigns/{campaign_id}/tts")).toHaveLength(0);
  rerender(view("first", [history, fresh, privateEvent]));
  await waitFor(() =>
    expect(backend.callsTo("POST /campaigns/{campaign_id}/tts")).toHaveLength(
      1,
    ),
  );
  expect(backend.callsTo("POST /campaigns/{campaign_id}/tts")[0]!.body).toEqual(
    { event_id: "fresh" },
  );
  rerender(view("second", [history, fresh, privateEvent]));
  await act(async () =>
    answer({ clip_id: "opaque", audio_base64: "UklGRg==" }),
  );
  expect(audioPlay).not.toHaveBeenCalled();
  expect(speak).not.toHaveBeenCalled();
  unmount();
});
