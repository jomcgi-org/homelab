/** The DM's writes to the read-aloud voice map, and the NPC search behind it. */
import { api } from "./client";
import type { VoiceRequest } from "./schema";

/**
 * A speaker key is `narrator`, an NPC's entity id or a free label. A label of
 * only dots is refused: it would resolve to a different path.
 */
export const SPEAKER_KEY = /^(?!\.+$)[A-Za-z0-9 _'.-]{1,64}$/;

/** What the server accepts in a preset. */
export const VOICE_LIMITS = {
  lang: 35,
  names: 8,
  name: 64,
  rate: { min: 0.5, max: 2 },
  pitch: { min: 0, max: 2 },
};

/** An NPC as the DM's entity search lists it. */
export interface NpcMatch {
  id: string;
  name: string;
  source_book?: string | null;
  source_type?: string | null;
}

export interface NpcSearch {
  items: NpcMatch[];
  total: number;
}

const NPC_PAGE = 30;

export const voiceKeys = {
  npcs: (campaignId: string, name: string) =>
    ["campaign", campaignId, "npc-search", name] as const,
};

/** A name search over the NPCs the DM can see, so no poll carries them all. */
export const searchNpcs = async (
  campaignId: string,
  name: string,
): Promise<NpcSearch> =>
  (await api("GET /campaigns/{campaign_id}/entities", {
    params: { campaign_id: campaignId },
    query: { q: name, type: "npc", limit: NPC_PAGE },
  })) as unknown as NpcSearch;

export const saveVoice = (input: {
  campaignId: string;
  speakerKey: string;
  voice: VoiceRequest;
}) =>
  api("PUT /campaigns/{campaign_id}/voices/{speaker_key}", {
    params: { campaign_id: input.campaignId, speaker_key: input.speakerKey },
    body: input.voice,
  });

export const deleteVoice = (input: {
  campaignId: string;
  speakerKey: string;
}) =>
  api("DELETE /campaigns/{campaign_id}/voices/{speaker_key}", {
    params: { campaign_id: input.campaignId, speaker_key: input.speakerKey },
  });
