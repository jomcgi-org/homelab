/** Invented turn order and voice data, and the routes that serve them in tests and the preview. */
import type { DmVoiceView } from "../../api/schema";
import type { TurnOrder } from "../../api/session";
import type { NpcSearch } from "../../api/voices";
import type { Routes } from "../backend";
import { ids } from "../fixtures";

export const npcIds = {
  warden: "77777777-7777-4777-8777-777777777777",
  gone: "88888888-8888-4888-8888-888888888888",
  ostler: "66666666-6666-4666-8666-666666666666",
};

/** The DM's view: the hidden entry keeps its real name. */
export const dmOrder: NonNullable<TurnOrder> = {
  round: 2,
  active_index: 0,
  hidden_display: "mask",
  entries: [
    {
      label: "Brannoch Stone",
      player_character_id: ids.spare,
      initiative: 17,
      hidden: false,
    },
    {
      label: "Cellar rats",
      player_character_id: null,
      initiative: 12,
      hidden: false,
    },
    {
      label: "The Pale Warden",
      player_character_id: null,
      initiative: 9,
      hidden: true,
    },
  ],
};

export const emptyOrder: NonNullable<TurnOrder> = {
  round: 1,
  active_index: null,
  entries: [],
};

const voice = (
  overrides: Partial<DmVoiceView> & Pick<DmVoiceView, "speaker_key">,
): DmVoiceView => ({
  voice_hint: { lang: null, names: [] },
  rate: 1,
  pitch: 1,
  updated_at: "2026-11-09T18:00:00Z",
  speaker_name: null,
  ...overrides,
});

export const voices: DmVoiceView[] = [
  voice({
    speaker_key: "narrator",
    voice_hint: { lang: "en-GB", names: ["English", "North"] },
    rate: 0.9,
  }),
  voice({
    speaker_key: npcIds.warden,
    speaker_name: "The Pale Warden",
    pitch: 0.4,
  }),
  voice({ speaker_key: "Innkeeper", rate: 1.2 }),
];

export const npcMatches: NpcSearch = {
  items: [
    { id: npcIds.ostler, name: "Odda the Ostler", source_type: "homebrew" },
  ],
  total: 1,
};

const asRecord = (order: TurnOrder) => order as Record<string, unknown>;

export const turnsRoutes: Routes = {
  "GET /campaigns/{campaign_id}/sessions/{session_id}/initiative":
    asRecord(dmOrder),
  "PUT /campaigns/{campaign_id}/sessions/{session_id}/initiative": (call) =>
    call.body as Record<string, unknown>,
  "POST /campaigns/{campaign_id}/sessions/{session_id}/initiative/advance":
    asRecord({ ...dmOrder, active_index: 1 }),
  "DELETE /campaigns/{campaign_id}/sessions/{session_id}/initiative":
    asRecord(emptyOrder),
  "GET /campaigns/{campaign_id}/voices": voices,
  "PUT /campaigns/{campaign_id}/voices/{speaker_key}": (call) =>
    voice({
      ...(call.body as Partial<DmVoiceView>),
      speaker_key: call.params.speaker_key!,
    }),
  "DELETE /campaigns/{campaign_id}/voices/{speaker_key}": { status: 204 },
};
