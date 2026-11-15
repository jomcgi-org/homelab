/** Invented session data for the table screen. */
import type { GameSessionView, SessionEventView } from "../../api/schema";
import type { Routes } from "../backend";
import { characters, ids, members } from "../fixtures";

export const sessionId = "55555555-5555-4555-8555-555555555555";

export const activeSession: GameSessionView = {
  id: sessionId,
  campaign_id: ids.owned,
  status: "active",
  started_at: "2026-11-10T19:00:00Z",
  ended_at: null,
};

let nextEvent = 0;
export const event = (
  overrides: Partial<SessionEventView> &
    Pick<SessionEventView, "kind" | "body">,
): SessionEventView => {
  nextEvent += 1;
  return {
    id: `e0000000-0000-4000-8000-${String(nextEvent).padStart(12, "0")}`,
    campaign_id: ids.owned,
    session_id: sessionId,
    seq: nextEvent,
    audience: "table",
    audience_pc_ids: [],
    author_member_id: null,
    created_at: "2026-11-10T19:05:00Z",
    retracted_at: null,
    ...overrides,
  };
};

export const story: SessionEventView[] = [
  event({
    kind: "narration",
    body: { text: "Rain hammers the shutters of the Gilded Eel." },
  }),
  event({
    kind: "action",
    audience: "dm",
    author_member_id: ids.playerMember,
    body: { text: "I check the cellar door for a draught." },
  }),
  event({
    kind: "roll",
    body: {
      total: 17,
      label: "Perception",
      formula: "1d20+3",
      rolls: [14],
      kept: [14],
    },
  }),
  event({
    kind: "turn",
    body: {
      action: "set",
      round: 1,
      active_index: 0,
      entries: [{ label: "Brannoch Stone" }, { label: "Cellar rats" }],
    },
  }),
];

/** Routes for the session screen as the DM of the owned campaign. */
export const sessionRoutes: Routes = {
  "GET /campaigns/{campaign_id}/sessions": [activeSession],
  "GET /campaigns/{campaign_id}/voices": [],
  "GET /campaigns/{campaign_id}/characters/{player_character_id}/sheets": {
    character: characters[0]!,
    viewer_role: "dm",
    versions: [],
  },
  "GET /campaigns/{campaign_id}/sessions/{session_id}/events": (call) =>
    story.filter((row) => row.seq > Number(call.query.get("after") ?? 0)),
  "GET /campaigns/{campaign_id}/sessions/{session_id}/initiative": {},
  "GET /campaigns/{campaign_id}/sessions/{session_id}/journal": {},
  // The notes panel stays mounted behind the Story tab.
  "GET /campaigns/{campaign_id}/notes": [],
  "GET /campaigns/{campaign_id}/members": [
    members[0]!,
    {
      ...members[1]!,
      player_character_id: ids.spare,
      character_name: "Brannoch Stone",
    },
  ],
};
