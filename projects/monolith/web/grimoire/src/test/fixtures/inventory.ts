/** Invented inventory data and the routes that serve it in tests and the preview. */
import type {
  InventoryCreateRequest,
  InventoryMoveRequest,
  InventoryPatchRequest,
} from "../../api/schema";
import type { InventoryChange, InventoryItem } from "../../api/inventory";
import type { Call, Routes } from "../backend";
import { ids } from "../fixtures";

export const inventoryIds = {
  rope: "1a000000-0000-4000-8000-000000000001",
  potion: "1a000000-0000-4000-8000-000000000002",
  ledger: "1a000000-0000-4000-8000-000000000003",
  moonRope: "1b000000-0000-4000-8000-000000000001",
};

const item = (
  overrides: Partial<InventoryItem> & Pick<InventoryItem, "id" | "name">,
): InventoryItem => ({
  owner: "party",
  is_mine: false,
  quantity: 1,
  notes: "",
  hidden_from_party: false,
  entity: null,
  created_at: "2026-11-10T19:10:00+00:00",
  updated_at: "2026-11-10T19:10:00+00:00",
  ...overrides,
});

/** The DM's view: the pool, one character's pack and an item the party cannot see. */
export const inventoryItems: InventoryItem[] = [
  item({ id: inventoryIds.potion, name: "Potion of healing", quantity: 3 }),
  item({
    id: inventoryIds.ledger,
    name: "Smuggler's ledger",
    notes: "Names half the harbour watch.",
    hidden_from_party: true,
  }),
  item({
    id: inventoryIds.rope,
    owner: ids.spare,
    name: "Silk rope",
    quantity: 5,
    notes: "Fifty feet.\nKnotted every arm's length.",
    entity: { id: inventoryIds.moonRope, name: "Moon rope", type: "item" },
  }),
];

/** What the character who holds the rope sees: no hidden pool item, and the rope is theirs. */
export const playerInventoryItems: InventoryItem[] = inventoryItems
  .filter((row) => row.owner !== "party" || !row.hidden_from_party)
  .map((row) => ({ ...row, is_mine: row.owner === ids.spare }));

let nextChange = 0;
const change = (
  overrides: Partial<InventoryChange> &
    Pick<InventoryChange, "item_id" | "action">,
): InventoryChange => {
  nextChange += 1;
  return {
    id: `1c000000-0000-4000-8000-${String(nextChange).padStart(12, "0")}`,
    delta: 0,
    quantity_after: 1,
    reason: "",
    session_id: null,
    event_id: null,
    created_at: "2026-11-10T19:10:00+00:00",
    is_mine: true,
    changes: {},
    ...overrides,
  };
};

export const inventoryHistory: InventoryChange[] = [
  change({
    item_id: inventoryIds.rope,
    action: "move",
    delta: -2,
    quantity_after: 5,
    reason: "Shared with the party",
    changes: { owner: { from: ids.spare, to: "party" } },
    created_at: "2026-11-10T19:20:00+00:00",
  }),
  change({
    item_id: inventoryIds.rope,
    action: "create",
    delta: 7,
    quantity_after: 7,
  }),
];

// The schema types inventory responses as open records.
const wire = (value: object) => value as Record<string, unknown>;

interface StoreOptions {
  viewer?: string;
  history?: InventoryChange[];
}

/**
 * Inventory routes over an in-memory copy of `seed`, so a write shows up in
 * the next read the way it does against the real API.
 */
export function inventoryStore(
  seed: InventoryItem[] = inventoryItems,
  /** `viewer` is the character the caller plays; the DM has none. */
  { viewer, history = inventoryHistory }: StoreOptions = {},
): Routes {
  let items = seed.map((row) => ({ ...row }));
  const changes = [...history];
  let nextItem = 0;
  const newId = () => {
    nextItem += 1;
    return `1d000000-0000-4000-8000-${String(nextItem).padStart(12, "0")}`;
  };
  const record = (
    row: InventoryItem,
    action: InventoryChange["action"],
    delta: number,
    reason: string | undefined,
    fields: InventoryChange["changes"] = {},
  ) =>
    changes.unshift(
      change({
        item_id: row.id,
        action,
        delta,
        quantity_after: row.quantity,
        reason: reason ?? "",
        changes: fields,
      }),
    );
  const find = (call: Call) =>
    items.find((row) => row.id === call.params.item_id);
  const missing = { status: 404, detail: "item not found" };

  return {
    "GET /campaigns/{campaign_id}/inventory": () => items.map(wire),
    "GET /campaigns/{campaign_id}/inventory/changes": (call) =>
      changes
        .filter((row) => row.item_id === call.query.get("item_id"))
        .map(wire),
    "POST /campaigns/{campaign_id}/inventory": (call) => {
      const body = call.body as InventoryCreateRequest;
      const row = item({
        id: newId(),
        owner: body.owner,
        name: body.name,
        quantity: body.quantity ?? 1,
        notes: body.notes ?? "",
        hidden_from_party: body.hidden_from_party ?? false,
      });
      items.push(row);
      record(row, "create", row.quantity, body.reason);
      return wire(row);
    },
    "PATCH /campaigns/{campaign_id}/inventory/{item_id}": (call) => {
      const row = find(call);
      if (!row) return missing;
      const { reason, ...fields } = call.body as InventoryPatchRequest;
      const before = row.quantity;
      Object.assign(row, fields);
      record(row, "update", row.quantity - before, reason);
      return wire(row);
    },
    "POST /campaigns/{campaign_id}/inventory/{item_id}/move": (call) => {
      const row = find(call);
      if (!row) return missing;
      const body = call.body as InventoryMoveRequest;
      const quantity = body.quantity ?? row.quantity;
      if (quantity < 1 || quantity > row.quantity)
        return {
          status: 422,
          detail: "move quantity exceeds available inventory",
        };
      const owner = { from: row.owner, to: body.owner };
      if (quantity === row.quantity) {
        row.owner = body.owner;
        row.is_mine = body.owner === viewer;
        record(row, "move", 0, body.reason, { owner });
        return wire(row);
      }
      row.quantity -= quantity;
      const moved = {
        ...row,
        id: newId(),
        owner: body.owner,
        is_mine: body.owner === viewer,
        quantity,
      };
      items.push(moved);
      record(row, "move", -quantity, body.reason, { owner });
      record(moved, "move", quantity, body.reason, { owner });
      return wire(moved);
    },
    "DELETE /campaigns/{campaign_id}/inventory/{item_id}": (call) => {
      items = items.filter((row) => row.id !== call.params.item_id);
      return undefined;
    },
  };
}

export const inventoryRoutes: Routes = inventoryStore();
