/** Invented knowledge data and the routes that serve it in tests and the preview. */
import type { EntityProjection, KnowledgeHit } from "../../api/knowledge";
import type { BulkGrantRequest, GrantView } from "../../api/schema";
import type { Routes } from "../backend";
import { ids } from "../fixtures";
import { sessionId } from "./session";

export const entityIds = {
  keeper: "61111111-1111-4111-8111-111111111111",
  road: "62222222-2222-4222-8222-222222222222",
  key: "63333333-3333-4333-8333-333333333333",
};

const spine = (
  id: string,
  name: string,
  entity_type: string,
  is_global: boolean,
) => ({
  id,
  name,
  entity_type,
  is_global,
  source_type: is_global ? "extracted" : "homebrew",
  source_book: is_global ? "Example Gazetteer" : null,
  created_in_session: null,
  created_at: "2026-10-03T18:00:00Z",
});

export const keeperGrant: GrantView = {
  id: "71111111-1111-4111-8111-111111111111",
  campaign_id: ids.owned,
  entity_id: entityIds.keeper,
  player_character_id: ids.spare,
  grant_scope: "partial",
  revealed_details: { disposition: "wary", clue: "Counts the silver twice." },
  granted_in_session: sessionId,
  created_at: "2026-11-10T19:10:00Z",
};

/** Entities as the DM reads them: every field, plus who holds a grant. */
export const dmEntities: EntityProjection[] = [
  {
    ...spine(entityIds.keeper, "Orla Fenn", "npc", false),
    occupation: "Keeper of the Gilded Eel",
    disposition: "wary",
    description: "Runs the inn and **misses nothing** said in it.",
    grants: [
      {
        id: keeperGrant.id,
        player_character_id: keeperGrant.player_character_id,
        grant_scope: keeperGrant.grant_scope,
        revealed_details: keeperGrant.revealed_details,
      },
    ],
  },
  {
    ...spine(entityIds.road, "The Old Marsh Road", "location", true),
    description: "A raised causeway, flooded at spring tide.",
    grants: [],
  },
  {
    ...spine(entityIds.key, "Cellar key", "item", false),
    description: "Iron, with a fish stamped on the bow.",
    grants: [],
  },
];

const SPINE_KEYS = new Set(Object.keys(spine("", "", "", false)));

/** The grantee projection the backend would return for one previewed grant. */
export function projectFor(
  entity: EntityProjection,
  grant: BulkGrantRequest["grants"][number],
): EntityProjection {
  const identity = {
    id: entity.id,
    name: entity.name,
    entity_type: entity.entity_type,
  };
  if (grant.grant_scope === "name_only")
    return { ...identity, recognition_only: true };
  if (grant.grant_scope === "partial")
    return { ...identity, revealed_details: grant.revealed_details ?? {} };
  const { grants: _grants, ...full } = entity;
  return full;
}

/** What a player's search returns: one hit of each type. */
export const searchHits: KnowledgeHit[] = [
  {
    type: "entity",
    id: entityIds.keeper,
    name: "Orla Fenn",
    entity_type: "npc",
    revealed_details: keeperGrant.revealed_details,
    source: { entity_id: entityIds.keeper },
  },
  {
    type: "note",
    id: "81111111-1111-4111-8111-111111111111",
    title: "Cellar door",
    preview: "The draught comes from under the third barrel.",
    source: { note_id: "81111111-1111-4111-8111-111111111111" },
  },
  {
    type: "event",
    id: "e0000000-0000-4000-8000-000000000001",
    kind: "narration",
    preview: "Rain hammers the shutters of the Gilded Eel.",
    source: { session_id: sessionId, seq: 1 },
  },
  {
    type: "chunk",
    id: "gazetteer-0042",
    display_name: "Example Gazetteer",
    preview: "The marsh road floods at spring tide.",
    source: { book_id: "example-gazetteer", chunk_id: "gazetteer-0042" },
  },
  {
    type: "fact",
    id: "91111111-1111-4111-8111-111111111111",
    preview: "The innkeeper owes the harbour master a favour.",
    status: "active",
    ownership: "you",
    can_dispute: true,
    source: { fact_id: "91111111-1111-4111-8111-111111111111" },
  },
];

const grantView = (
  grant: BulkGrantRequest["grants"][number],
  index: number,
): GrantView => ({
  id: `7a000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
  campaign_id: ids.owned,
  entity_id: grant.entity_id,
  player_character_id: grant.player_character_id,
  grant_scope: grant.grant_scope,
  revealed_details: grant.revealed_details ?? null,
  granted_in_session: sessionId,
  created_at: "2026-11-10T19:20:00Z",
});

/** Knowledge routes as the DM of the owned campaign. */
export const knowledgeRoutes: Routes = {
  "GET /campaigns/{campaign_id}/entities": (call) => {
    const q = (call.query.get("q") ?? "").toLowerCase();
    const items = dmEntities
      .filter((row) => row.name.toLowerCase().includes(q))
      .map((row) =>
        Object.fromEntries(
          Object.entries(row).filter(
            ([key]) => SPINE_KEYS.has(key) || key === "grants",
          ),
        ),
      );
    return { items, total: items.length, next_cursor: null };
  },
  "GET /campaigns/{campaign_id}/entities/{entity_id}": (call) =>
    dmEntities.find((row) => row.id === call.params.entity_id) ?? {
      status: 404,
      detail: "entity not found",
    },
  "GET /campaigns/{campaign_id}/grants": [keeperGrant],
  "POST /campaigns/{campaign_id}/grants/preview": (call) =>
    (call.body as BulkGrantRequest).grants.map((grant) => ({
      player_character_id: grant.player_character_id,
      projection: projectFor(
        dmEntities.find((row) => row.id === grant.entity_id)!,
        grant,
      ),
    })),
  "POST /campaigns/{campaign_id}/grants/bulk": (call) =>
    (call.body as BulkGrantRequest).grants.map(grantView),
  "PATCH /campaigns/{campaign_id}/grants/{grant_id}": keeperGrant,
  "DELETE /campaigns/{campaign_id}/grants/{grant_id}": undefined,
  "GET /campaigns/{campaign_id}/knowledge/search": searchHits,
  "POST /campaigns/{campaign_id}/facts/{fact_id}/dispute": undefined,
};
