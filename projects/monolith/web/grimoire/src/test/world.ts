/**
 * A complete fixture backend: every route a screen calls, answered with
 * invented data. The fixture preview mounts the real app against it, so each
 * screen can be opened and screenshotted with no backend and no sign-in.
 * Add a route here when a screen starts calling a new operation.
 */
import type { OtherRoutes, Routes } from "./backend";
import { characters, invitation, lobby, members } from "./fixtures";
import { sessionRoutes } from "./fixtures/session";
import { feedbackRoutes } from "./fixtures/feedback";
import { handoutsRoutes } from "./fixtures/handouts";
import { inventoryRoutes } from "./fixtures/inventory";
import { joinOther } from "./fixtures/join";
import { journalRoutes } from "./fixtures/journal";
import { knowledgeRoutes } from "./fixtures/knowledge";
import { notesRoutes } from "./fixtures/notes";
import { platformOther } from "./fixtures/platform";
import { sheetsRoutes } from "./fixtures/sheets";
import { turnsRoutes } from "./fixtures/turns";

export const world: Routes = {
  "POST /campaigns/{campaign_id}/tts": { status: 404 },
  "GET /campaigns/{campaign_id}/tts/{event_id}/{clip_id}": { status: 404 },
  "GET /lobby": lobby(),
  "GET /campaigns/{campaign_id}/members": members,
  "GET /campaigns/{campaign_id}/characters": characters,
  "GET /campaigns/{campaign_id}/invitations": [
    { ...invitation, invitee_email: "wren@example.test" },
  ],
  "GET /campaigns/{campaign_id}/join-links": { status: 503 },
  ...sessionRoutes,
  ...feedbackRoutes,
  ...handoutsRoutes,
  ...inventoryRoutes,
  ...journalRoutes,
  ...knowledgeRoutes,
  ...notesRoutes,
  ...sheetsRoutes,
  ...turnsRoutes,
};

/** The endpoints outside the Grimoire API: the join landing and accounts. */
export const worldOther: OtherRoutes = { ...joinOther, ...platformOther };
