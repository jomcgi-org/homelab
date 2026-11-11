import { useState } from "react";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import {
  inventoryKeys,
  NAME_MAX,
  NOTES_MAX,
  QUANTITY_MAX,
  REASON_MAX,
  updateItem,
  type InventoryItem,
} from "../api/inventory";
import { useAction } from "../app/useAction";
import { FormDialog } from "./FormDialog";
import { validQuantity } from "./owners";

interface EditItemDialogProps {
  campaignId: string;
  item: InventoryItem;
  dm: boolean;
  onClose: () => void;
}

/**
 * The DM edits everything about an item. A player may only change how many
 * of their own item they hold, down to none.
 */
export function EditItemDialog({
  campaignId,
  item,
  dm,
  onClose,
}: EditItemDialogProps) {
  const [name, setName] = useState(item.name);
  const [notes, setNotes] = useState(item.notes);
  const [hidden, setHidden] = useState(item.hidden_from_party);
  const [quantity, setQuantity] = useState<number | null>(item.quantity);
  const [reason, setReason] = useState("");
  const save = useAction(updateItem, {
    invalidate: [inventoryKeys.all(campaignId)],
    onSuccess: onClose,
  });
  const trimmed = name.trim();
  const valid =
    validQuantity(quantity, 0) &&
    (!dm || (trimmed !== "" && notes.length <= NOTES_MAX));
  const submit = () => {
    if (!valid || save.isPending) return;
    save.run({
      campaignId,
      itemId: item.id,
      quantity,
      reason,
      details: dm
        ? { name: trimmed, notes, hiddenFromParty: hidden }
        : undefined,
    });
  };
  return (
    <FormDialog
      title={dm ? `Edit ${item.name}` : `Adjust quantity for ${item.name}`}
      submitLabel="Save"
      isPending={save.isPending}
      isInvalid={!valid}
      onSubmit={submit}
      onClose={onClose}
    >
      {dm && (
        <>
          <TextInput
            label="Name"
            value={name}
            onChange={(value) => setName(value.slice(0, NAME_MAX))}
            isRequired
            autoComplete="off"
            width="100%"
          />
          <TextArea
            label="Notes"
            value={notes}
            onChange={setNotes}
            maxLength={NOTES_MAX}
            width="100%"
          />
          <CheckboxInput
            label="Hidden from party"
            value={hidden}
            onChange={setHidden}
          />
        </>
      )}
      <NumberInput
        label="Quantity"
        value={quantity}
        onChange={setQuantity}
        min={0}
        max={QUANTITY_MAX}
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
