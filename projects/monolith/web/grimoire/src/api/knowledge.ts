/**
 * Knowledge: what a viewer may see of an entity, the search across it, and the
 * DM's grants that decide both.
 *
 * Every entity read is already projected by the backend for the caller. The
 * UI renders what a projection carries and nothing else, so these types keep
 * the open-ended detail fields as `unknown`.
 */
import { useQuery } from "@tanstack/react-query";
import { api, ApiError, SignInError } from "./client";
import type { BulkGrantItem, GrantView } from "./schema";

export type GrantScope = GrantView["grant_scope"];
export type RevealedDetails = Record<string, unknown>;

/** A grant as the DM's entity reads annotate it. */
export interface EntityGrant {
  id: string;
  player_character_id: string;
  grant_scope: GrantScope;
  revealed_details: RevealedDetails | null;
}

/**
 * One entity as the caller may see it. A full projection carries the spine and
 * typed detail; a partial one only identity and `revealed_details`; a
 * name-only stub sets `recognition_only`. The DM also gets `grants`.
 */
export interface EntityProjection {
  id: string;
  name: string;
  entity_type: string;
  is_global?: boolean;
  recognition_only?: boolean;
  revealed_details?: RevealedDetails | null;
  grants?: EntityGrant[];
  [field: string]: unknown;
}

interface EntityPage {
  items: EntityProjection[];
  total: number;
  next_cursor: string | null;
}

/** What one recipient would see if the previewed grant were saved. */
export interface GrantPreview {
  player_character_id: string;
  projection: EntityProjection;
}

type Hit<TType extends string, TSource> = {
  type: TType;
  id: string;
  preview?: string;
  source: TSource;
};

export type EntityHit = Hit<"entity", { entity_id: string }> & {
  name: string;
  entity_type: string;
  revealed_details?: RevealedDetails | null;
};
export type NoteHit = Hit<"note", { note_id: string }> & { title: string };
export type EventHit = Hit<"event", { session_id: string; seq: number }> & {
  kind: string;
};
export type ChunkHit = Hit<"chunk", { book_id: string; chunk_id: string }> & {
  display_name: string;
};
export type FactHit = Hit<"fact", { fact_id: string }> & {
  status: "active" | "disputed";
  ownership: "you" | "party" | "character";
  can_dispute: boolean;
};
export type KnowledgeHit = EntityHit | NoteHit | EventHit | ChunkHit | FactHit;

const UUID = /^[0-9a-f-]{36}$/i;
export const isEntityId = (value: string) => UUID.test(value);

const SEARCH_UNAVAILABLE = "Knowledge search is unavailable.";
const ENTITY_PICKER_LIMIT = 30;
const ENTITY_PAGE = 500;

export const knowledgeKeys = {
  /** Everything a grant change can alter. */
  all: (campaignId: string) => ["campaign", campaignId, "knowledge"] as const,
  entity: (campaignId: string, entityId: string) =>
    ["campaign", campaignId, "knowledge", "entity", entityId] as const,
  entities: (campaignId: string) =>
    ["campaign", campaignId, "knowledge", "entities"] as const,
  grants: (campaignId: string) =>
    ["campaign", campaignId, "knowledge", "grants"] as const,
  search: (campaignId: string, query: string, attempt: number) =>
    ["campaign", campaignId, "knowledge", "search", query, attempt] as const,
};

/** What a reveal, grant edit or retraction refetches: the feed and all knowledge. */
export const afterGrantChange = (campaignId: string) => [
  ["campaign", campaignId, "session"] as const,
  knowledgeKeys.all(campaignId),
];

/**
 * One entity as the caller may see it. Nothing is kept once the last reader
 * leaves, so a revoked entry is never shown again from memory.
 */
export const useEntity = (
  campaignId: string,
  entityId: string | null,
  refetchInterval: number | false = false,
) =>
  useQuery({
    queryKey: knowledgeKeys.entity(campaignId, entityId ?? ""),
    enabled: entityId !== null && isEntityId(entityId),
    queryFn: async () =>
      (await api("GET /campaigns/{campaign_id}/entities/{entity_id}", {
        params: { campaign_id: campaignId, entity_id: entityId! },
      })) as EntityProjection,
    refetchInterval,
    staleTime: 0,
    gcTime: 0,
  });

/** The DM's entity picker: names matching `q`, optionally hiding what one character already knows. */
export async function findEntities(input: {
  campaignId: string;
  q: string;
  notGrantedTo: string | null;
  signal?: AbortSignal;
}): Promise<EntityProjection[]> {
  const page = (await api("GET /campaigns/{campaign_id}/entities", {
    params: { campaign_id: input.campaignId },
    query: {
      q: input.q,
      limit: ENTITY_PICKER_LIMIT,
      not_granted_to: input.notGrantedTo,
    },
    signal: input.signal,
  })) as unknown as EntityPage;
  return page.items;
}

/** Every entity in the campaign at spine level, for the DM's grants table. */
export const useAllEntities = (campaignId: string, enabled: boolean) =>
  useQuery({
    queryKey: knowledgeKeys.entities(campaignId),
    enabled,
    queryFn: async ({ signal }) => {
      const found: EntityProjection[] = [];
      let cursor: string | null = null;
      do {
        const page = (await api("GET /campaigns/{campaign_id}/entities", {
          params: { campaign_id: campaignId },
          query: { limit: ENTITY_PAGE, cursor },
          signal,
        })) as unknown as EntityPage;
        found.push(...page.items);
        cursor = page.next_cursor;
      } while (cursor);
      return found;
    },
  });

export const useGrants = (campaignId: string, enabled: boolean) =>
  useQuery({
    queryKey: knowledgeKeys.grants(campaignId),
    enabled,
    queryFn: () =>
      api("GET /campaigns/{campaign_id}/grants", {
        params: { campaign_id: campaignId },
      }),
  });

/** A member's search across the entities, notes, events, lore and facts they may see. */
export async function searchKnowledge(input: {
  campaignId: string;
  q: string;
  signal?: AbortSignal;
}): Promise<KnowledgeHit[]> {
  const hits: unknown = await api(
    "GET /campaigns/{campaign_id}/knowledge/search",
    {
      params: { campaign_id: input.campaignId },
      query: { q: input.q },
      signal: input.signal,
    },
  );
  // Anything but a list fails closed instead of rendering a guess.
  if (!Array.isArray(hits)) throw new ApiError(502, SEARCH_UNAVAILABLE);
  return hits as KnowledgeHit[];
}

/** Marks the caller's own fact as disputed. A refusal never echoes backend detail. */
export async function disputeFact(input: {
  campaignId: string;
  factId: string;
}) {
  try {
    await api("POST /campaigns/{campaign_id}/facts/{fact_id}/dispute", {
      params: { campaign_id: input.campaignId, fact_id: input.factId },
    });
  } catch (error) {
    if (error instanceof ApiError && !(error instanceof SignInError))
      throw new ApiError(
        error.status,
        "The fact could not be disputed. Try again.",
      );
    throw error;
  }
}

export interface GrantDraft {
  campaignId: string;
  entityId: string;
  characterIds: string[];
  scope: GrantScope;
  /** Sent only for a partial grant; other scopes carry no detail. */
  details: RevealedDetails;
}

const grantItems = (draft: GrantDraft): BulkGrantItem[] =>
  draft.characterIds.map((id) => ({
    entity_id: draft.entityId,
    player_character_id: id,
    grant_scope: draft.scope,
    revealed_details: draft.scope === "partial" ? draft.details : null,
  }));

/** The projection each recipient would get. Saves nothing. */
export const previewGrants = async (draft: GrantDraft) =>
  (await api("POST /campaigns/{campaign_id}/grants/preview", {
    params: { campaign_id: draft.campaignId },
    body: { grants: grantItems(draft) },
  })) as unknown as GrantPreview[];

/** Grants the entity to each character and, in a live session, tells them. */
export const revealGrants = (draft: GrantDraft) =>
  api("POST /campaigns/{campaign_id}/grants/bulk", {
    params: { campaign_id: draft.campaignId },
    body: { grants: grantItems(draft) },
  });

export const updateGrant = (input: {
  campaignId: string;
  grantId: string;
  scope: GrantScope;
  details: RevealedDetails;
}) =>
  api("PATCH /campaigns/{campaign_id}/grants/{grant_id}", {
    params: { campaign_id: input.campaignId, grant_id: input.grantId },
    body: {
      grant_scope: input.scope,
      // An omitted or null value keeps the old detail, so leaving partial
      // scope sends an empty object to clear it.
      revealed_details: input.scope === "partial" ? input.details : {},
    },
  });

/** Retracts a grant. A silent retraction leaves no entity identity in the feed. */
export const revokeGrant = (input: {
  campaignId: string;
  grantId: string;
  silent: boolean;
}) =>
  api("DELETE /campaigns/{campaign_id}/grants/{grant_id}", {
    params: { campaign_id: input.campaignId, grant_id: input.grantId },
    query: { silent: input.silent },
  });
