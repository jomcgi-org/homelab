import { expect, it, vi } from "vitest";
import { api } from "./client";
import { loadEventAudio } from "./tts";

vi.mock("./client", () => ({ api: vi.fn() }));

it("decodes authorized speech into a WAV Blob before playback", async () => {
  const bytes = new Uint8Array([82, 73, 70, 70, 0, 128, 255]);
  vi.mocked(api).mockResolvedValue({
    clip_id: "clip",
    audio_base64: btoa(String.fromCharCode(...bytes)),
  });
  const signal = new AbortController().signal;
  const audio = await loadEventAudio("campaign", "event", signal);
  expect(audio).toBeInstanceOf(Blob);
  expect(audio.type).toBe("audio/wav");
  expect(new Uint8Array(await audio.arrayBuffer())).toEqual(bytes);
  expect(api).toHaveBeenCalledWith("POST /campaigns/{campaign_id}/tts", {
    params: { campaign_id: "campaign" },
    body: { event_id: "event" },
    signal,
  });
});

it.each(["%%%", "A".repeat(2_796_208)])(
  "rejects invalid or oversized audio (%#)",
  async (audio_base64) => {
    vi.mocked(api).mockResolvedValue({ clip_id: "clip", audio_base64 });
    await expect(
      loadEventAudio("campaign", "event", new AbortController().signal),
    ).rejects.toThrow("Invalid audio");
  },
);
