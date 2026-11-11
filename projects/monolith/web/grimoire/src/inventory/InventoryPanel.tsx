import { useEffect, useState } from "react";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { Button } from "@astryxdesign/core/Button";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import {
  deleteItem,
  inventoryKeys,
  PARTY,
  useInventory,
  type InventoryItem,
  type Owner,
} from "../api/inventory";
import { Loaded } from "../app/Loaded";
import { useAction } from "../app/useAction";
import type { TableCharacter } from "../screens/session/table";
import { EditItemDialog } from "./EditItemDialog";
import { BLANK_GIVE, GiveItemDialog, type GiveStart } from "./GiveItemDialog";
import "./inventory.css";
import { ItemRow } from "./ItemRow";
import { MoveItemDialog } from "./MoveItemDialog";
import { ownerOptions, PARTY_POOL, type OwnerOption } from "./owners";

/** Values to prefill the give-item form, set from a feed event. */
export interface InventoryPrefill {
  name: string;
  entity_id: string | null;
  owner: string;
  source_event_id: string;
}

export interface InventoryPanelProps {
  campaignId: string;
  dm: boolean;
  characters: TableCharacter[];
  prefill: InventoryPrefill | null;
  onOpenKnowledge: (entityId: string) => void;
}

interface Section {
  key: string;
  name: string;
  holds: (item: InventoryItem) => boolean;
}

/**
 * The panel remounts each time its tab opens, still holding the last feed
 * event's prefill. Each prefill opens the give form once, so coming back to
 * the tab later does not offer the same item again.
 */
const offered = new WeakSet<InventoryPrefill>();

/** Party and character inventory, with the DM's give, move and remove. */
export function InventoryPanel({
  campaignId,
  dm,
  characters,
  prefill,
  onOpenKnowledge,
}: InventoryPanelProps) {
  const inventory = useInventory(campaignId);
  const [giving, setGiving] = useState<GiveStart | null>(null);
  const [editing, setEditing] = useState<InventoryItem | null>(null);
  const [moving, setMoving] = useState<InventoryItem | null>(null);
  const [deleting, setDeleting] = useState<InventoryItem | null>(null);
  const remove = useAction(
    (item: InventoryItem) => deleteItem({ campaignId, itemId: item.id }),
    {
      invalidate: [inventoryKeys.all(campaignId)],
      success: (_result, item) => `${item.name} deleted.`,
      onSuccess: () => setDeleting(null),
    },
  );

  useEffect(() => {
    if (!dm || !prefill || offered.has(prefill)) return;
    offered.add(prefill);
    setGiving(prefill);
  }, [dm, prefill]);

  const owners = ownerOptions(characters);
  // A player is only ever told about their own character.
  const ownCharacter = dm ? undefined : characters[0];
  const sections: Section[] = dm
    ? owners.map((owner) => ({
        key: owner.value,
        name: owner.label,
        holds: (item) => item.owner === owner.value,
      }))
    : [
        { key: PARTY, name: PARTY_POOL, holds: (item) => item.owner === PARTY },
        { key: "mine", name: "Your inventory", holds: (item) => item.is_mine },
      ];

  const destinations = (item: InventoryItem): OwnerOption[] => {
    if (dm) return owners.filter((owner) => owner.value !== item.owner);
    // The backend refuses a player's move of a hidden item, so none is offered.
    if (item.hidden_from_party || !ownCharacter) return [];
    if (item.owner === PARTY)
      return [{ value: ownCharacter.id, label: "Your inventory" }];
    return item.is_mine ? [{ value: PARTY, label: PARTY_POOL }] : [];
  };
  const ownerName = (owner: Owner | null) =>
    owner === null
      ? "none"
      : (characters.find((row) => row.id === owner)?.character_name ?? owner);

  return (
    <VStack gap={5} as="section" aria-label="Campaign inventory">
      {dm && (
        <HStack>
          <Button
            label="Give item"
            variant="primary"
            onClick={() => setGiving(BLANK_GIVE)}
          />
        </HStack>
      )}
      <Loaded query={inventory} what="the inventory">
        {(items) => (
          <>
            {sections.map((section) => {
              const held = items.filter(section.holds);
              return (
                <VStack
                  gap={2}
                  as="section"
                  key={section.key}
                  aria-label={section.name}
                >
                  <Heading level={2}>{section.name}</Heading>
                  {held.length === 0 ? (
                    <Text type="body" color="secondary">
                      No items here yet.
                    </Text>
                  ) : (
                    <ul className="inventory-list">
                      {held.map((item) => (
                        <ItemRow
                          key={item.id}
                          campaignId={campaignId}
                          item={item}
                          dm={dm}
                          canMove={destinations(item).length > 0}
                          ownerName={ownerName}
                          onEdit={() => setEditing(item)}
                          onMove={() => setMoving(item)}
                          onOpenKnowledge={onOpenKnowledge}
                        />
                      ))}
                    </ul>
                  )}
                </VStack>
              );
            })}
          </>
        )}
      </Loaded>
      {giving && (
        <GiveItemDialog
          campaignId={campaignId}
          start={giving}
          owners={owners}
          onOpenKnowledge={onOpenKnowledge}
          onClose={() => setGiving(null)}
        />
      )}
      {editing && (
        <EditItemDialog
          campaignId={campaignId}
          item={editing}
          dm={dm}
          onClose={() => setEditing(null)}
          onDelete={
            dm
              ? () => {
                  // One dialog at a time: the confirmation replaces the form.
                  setDeleting(editing);
                  setEditing(null);
                }
              : undefined
          }
        />
      )}
      {moving && (
        <MoveItemDialog
          campaignId={campaignId}
          item={moving}
          dm={dm}
          destinations={destinations(moving)}
          onClose={() => setMoving(null)}
        />
      )}
      <AlertDialog
        isOpen={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title={`Delete ${deleting?.name ?? "item"}?`}
        description="Its audit history is retained."
        actionLabel="Delete item"
        isActionLoading={remove.isPending}
        onAction={() => deleting && remove.run(deleting)}
      />
    </VStack>
  );
}
