/**
 * The party pool and each character's inventory.
 *
 * The backend decides who sees what: a player gets the visible party pool and
 * their own items, the DM gets everything. This module only shapes the calls.
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "./client";

/** The party pool, or the id of the character that holds the item. */
export type Owner = string;
export const PARTY: Owner = "party";

export const QUANTITY_MAX = 1_000_000;
export const NAME_MAX = 200;
export const NOTES_MAX = 20_000;
export const REASON_MAX = 500;

export interface InventoryEntity {
  id: string;
  name: string;
  type: string;
}

export interface InventoryItem {
  id: string;
  owner: Owner;
  /** Held by the viewer's own character. Never true for the DM. */
  is_mine: boolean;
  name: string;
  quantity: number;
  notes: string;
  hidden_from_party: boolean;
  /** Linked knowledge, when the viewer is allowed to recognise it. */
  entity: InventoryEntity | null;
  created_at: string;
  updated_at: string;
}

export interface InventoryChange {
  id: string;
  item_id: string;
  action: "create" | "update" | "move" | "delete";
  delta: number;
  quantity_after: number;
  reason: string;
  session_id: string | null;
  event_id: string | null;
  created_at: string;
  is_mine: boolean;
  /**
   * Field provenance. For a player, owner ids are already projected to
   * `party`, `you` or `character`; the DM receives character ids.
   */
  changes: {
    owner?: { from: string | null; to: string | null };
    [field: string]: unknown;
  };
  /** DM only. */
  who_member_id?: string | null;
}

export const inventoryKeys = {
  /** Prefix of every inventory query: invalidate this after a write. */
  all: (campaignId: string) => ["campaign", campaignId, "inventory"] as const,
  items: (campaignId: string) =>
    ["campaign", campaignId, "inventory", "items"] as const,
  changes: (campaignId: string, itemId: string) =>
    ["campaign", campaignId, "inventory", "changes", itemId] as const,
};

/** Other seats change the inventory too, so the list keeps itself current. */
const REFRESH_MS = 4_000;

export const useInventory = (campaignId: string) =>
  useQuery({
    queryKey: inventoryKeys.items(campaignId),
    queryFn: async () =>
      (await api("GET /campaigns/{campaign_id}/inventory", {
        params: { campaign_id: campaignId },
      })) as unknown as InventoryItem[],
    refetchInterval: REFRESH_MS,
  });

/** One item's audit history, newest first. */
export const useItemHistory = (campaignId: string, itemId: string) =>
  useQuery({
    queryKey: inventoryKeys.changes(campaignId, itemId),
    queryFn: async () =>
      (await api("GET /campaigns/{campaign_id}/inventory/changes", {
        params: { campaign_id: campaignId },
        query: { item_id: itemId },
      })) as unknown as InventoryChange[],
  });

export interface GiveItemInput {
  campaignId: string;
  owner: Owner;
  name: string;
  quantity: number;
  notes: string;
  hiddenFromParty: boolean;
  reason: string;
  entityId: string | null;
  sourceEventId: string | null;
}

/** DM only. */
export const giveItem = (input: GiveItemInput) =>
  api("POST /campaigns/{campaign_id}/inventory", {
    params: { campaign_id: input.campaignId },
    body: {
      owner: input.owner,
      name: input.name,
      quantity: input.quantity,
      notes: input.notes,
      entity_id: input.entityId,
      hidden_from_party: input.hiddenFromParty,
      reason: input.reason,
      source_event_id: input.sourceEventId,
    },
  });

export interface UpdateItemInput {
  campaignId: string;
  itemId: string;
  quantity: number;
  reason: string;
  /**
   * The DM's fields. A player leaves this out: the backend refuses a player's
   * patch that carries anything but a quantity.
   */
  details?: { name: string; notes: string; hiddenFromParty: boolean };
}

export const updateItem = (input: UpdateItemInput) =>
  api("PATCH /campaigns/{campaign_id}/inventory/{item_id}", {
    params: { campaign_id: input.campaignId, item_id: input.itemId },
    body: {
      quantity: input.quantity,
      reason: input.reason,
      ...(input.details && {
        name: input.details.name,
        notes: input.details.notes,
        hidden_from_party: input.details.hiddenFromParty,
      }),
    },
  });

export interface MoveItemInput {
  campaignId: string;
  itemId: string;
  owner: Owner;
  /** Fewer than the item holds splits it. */
  quantity: number;
  reason: string;
}

export const moveItem = (input: MoveItemInput) =>
  api("POST /campaigns/{campaign_id}/inventory/{item_id}/move", {
    params: { campaign_id: input.campaignId, item_id: input.itemId },
    body: {
      owner: input.owner,
      quantity: input.quantity,
      reason: input.reason,
    },
  });

/** DM only. The item leaves every list; its audit history is kept. */
export const deleteItem = (input: { campaignId: string; itemId: string }) =>
  api("DELETE /campaigns/{campaign_id}/inventory/{item_id}", {
    params: { campaign_id: input.campaignId, item_id: input.itemId },
  });
