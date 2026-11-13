import { api } from "./client";

/** Only event IDs cross this boundary. Audio always passes current backend ACLs. */
export async function loadEventAudio(
  campaignId: string,
  eventId: string,
  signal: AbortSignal,
): Promise<string> {
  const clip = await api("POST /campaigns/{campaign_id}/tts", {
    params: { campaign_id: campaignId },
    body: { event_id: eventId },
    signal,
  });
  const audio = await api(
    "GET /campaigns/{campaign_id}/tts/{event_id}/{clip_id}",
    {
      params: {
        campaign_id: campaignId,
        event_id: eventId,
        clip_id: clip.clip_id,
      },
      signal,
    },
  );
  // 2 MiB decoded. Data never becomes a public object-store URL.
  if (
    audio.audio_base64.length > 2_796_204 ||
    !/^[A-Za-z0-9+/]*={0,2}$/.test(audio.audio_base64)
  )
    throw new Error("Invalid audio");
  return `data:audio/wav;base64,${audio.audio_base64}`;
}
