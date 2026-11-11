import { useState } from "react";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import { Selector } from "@astryxdesign/core/Selector";
import { TextInput } from "@astryxdesign/core/TextInput";
import {
  inventoryKeys,
  moveItem,
  PARTY,
  REASON_MAX,
  type InventoryItem,
} from "../api/inventory";
import { useAction } from "../app/useAction";
import { FormDialog } from "./FormDialog";
import { validQuantity, type OwnerOption } from "./owners";

/** What a player calls their one permitted move; the DM always just moves. */
export const moveLabel = (item: InventoryItem, dm: boolean) =>
  dm
    ? `Move ${item.name}`
    : item.owner === PARTY
      ? `Take ${item.name}`
      : `Move ${item.name} to party pool`;

interface MoveItemDialogProps {
  campaignId: string;
  item: InventoryItem;
  dm: boolean;
  /** Where the item may go. A player has exactly one destination. */
  destinations: OwnerOption[];
  onClose: () => void;
}

/** Moves all of an item, or splits off part of it, to another owner. */
export function MoveItemDialog({
  campaignId,
  item,
  dm,
  destinations,
  onClose,
}: MoveItemDialogProps) {
  const [owner, setOwner] = useState(destinations[0]?.value ?? "");
  const [quantity, setQuantity] = useState<number | null>(item.quantity);
  const [reason, setReason] = useState("");
  const move = useAction(moveItem, {
    invalidate: [inventoryKeys.all(campaignId)],
    onSuccess: onClose,
  });
  const title = moveLabel(item, dm);
  const valid = owner !== "" && validQuantity(quantity, 1, item.quantity);
  const submit = () => {
    if (!valid || move.isPending) return;
    move.run({ campaignId, itemId: item.id, owner, quantity, reason });
  };
  return (
    <FormDialog
      title={title}
      submitLabel={title}
      isPending={move.isPending}
      isInvalid={!valid}
      onSubmit={submit}
      onClose={onClose}
    >
      {dm && (
        <Selector
          label="Move to"
          options={destinations}
          value={owner}
          onChange={setOwner}
          width="100%"
        />
      )}
      <NumberInput
        label="Quantity"
        description={
          item.quantity > 1
            ? `Of ${item.quantity}. Fewer splits the item.`
            : undefined
        }
        value={quantity}
        onChange={setQuantity}
        min={1}
        max={item.quantity}
        isIntegerOnly
        isRequired
        width="100%"
      />
      <TextInput
        label="Reason"
        description="Kept in the item's history."
        value={reason}
        onChange={(value) => setReason(value.slice(0, REASON_MAX))}
        isOptional
        width="100%"
      />
    </FormDialog>
  );
}
