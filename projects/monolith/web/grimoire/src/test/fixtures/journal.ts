/** Invented journal data and the routes that serve it in tests and the preview. */
import type { CampaignJournalView, Journal } from "../../api/schema";
import type { Routes } from "../backend";
import { ids } from "../fixtures";
import { sessionId } from "./session";

export const journalIds = {
  ferryman: "f1000000-0000-4000-8000-000000000001",
  smuggler: "f1000000-0000-4000-8000-000000000002",
  stranger: "f1000000-0000-4000-8000-000000000003",
  cellar: "f1000000-0000-4000-8000-000000000004",
  reveal: "e1000000-0000-4000-8000-000000000001",
  retractedReveal: "e1000000-0000-4000-8000-000000000002",
  recognition: "e1000000-0000-4000-8000-000000000003",
  handout: "e1000000-0000-4000-8000-000000000004",
  roll: "e1000000-0000-4000-8000-000000000005",
  action: "e1000000-0000-4000-8000-000000000006",
  earlierSession: "56565656-5656-4565-8565-565656565656",
};

/** One entry of every kind: explorable, retracted and name-only knowledge. */
export const sessionJournal: Journal = {
  learned: [
    {
      event_id: journalIds.reveal,
      entity_id: journalIds.ferryman,
      name: "Old Tam",
      entity_type: "npc",
      grant_scope: "partial",
      retracted: false,
      entity: { revealed_details: { occupation: "Ferryman" } },
    },
    {
      event_id: journalIds.retractedReveal,
      entity_id: journalIds.smuggler,
      name: "The Grey Hand",
      entity_type: "faction",
      grant_scope: "full",
      retracted: true,
    },
    {
      event_id: journalIds.recognition,
      entity_id: journalIds.stranger,
      name: "Sister Orla",
      entity_type: "npc",
      grant_scope: "name_only",
      retracted: false,
      entity: { recognition_only: true },
    },
  ],
  received: [
    {
      id: journalIds.handout,
      kind: "handout",
      body: {
        title: "Letter from the harbourmaster",
        markdown: "Meet me at **dusk**.\n\n- Bring the ledger\n- Come alone",
      },
    },
  ],
  people_and_places: [
    { id: journalIds.ferryman, name: "Old Tam", entity_type: "npc" },
    { id: journalIds.cellar, name: "The Gilded Eel", entity_type: "location" },
  ],
  rolls: [
    {
      id: journalIds.roll,
      kind: "roll",
      body: { label: "Perception", formula: "1d20+3", total: 17 },
    },
  ],
  open_threads: [
    {
      id: journalIds.action,
      kind: "action",
      body: { text: "I check the cellar door for a draught." },
    },
  ],
  truncated: false,
};

export const campaignJournal: CampaignJournalView = {
  sessions: [
    {
      session_id: sessionId,
      started_at: "2026-11-10T19:00:00Z",
      journal: sessionJournal,
    },
    {
      session_id: journalIds.earlierSession,
      started_at: "2026-11-03T19:00:00Z",
      journal: {},
    },
  ],
  next_cursor: null,
};

export const journalRoutes: Routes = {
  "GET /campaigns/{campaign_id}/sessions/{session_id}/journal": (call) =>
    call.query.get("view") === "party"
      ? {
          received: sessionJournal.received,
          people_and_places: sessionJournal.people_and_places,
          rolls: sessionJournal.rolls,
        }
      : sessionJournal,
  "GET /campaigns/{campaign_id}/journal": (call) =>
    call.params.campaign_id === ids.owned
      ? campaignJournal
      : { sessions: [], next_cursor: null },
};
