import { useState } from "react";
import { Button } from "@astryxdesign/core/Button";
import { CheckboxInput } from "@astryxdesign/core/CheckboxInput";
import { HStack } from "@astryxdesign/core/Layout";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import { Selector } from "@astryxdesign/core/Selector";
import { Text } from "@astryxdesign/core/Text";
import { TextArea } from "@astryxdesign/core/TextArea";
import { TextInput } from "@astryxdesign/core/TextInput";
import {
  giveItem,
  inventoryKeys,
  NAME_MAX,
  NOTES_MAX,
  PARTY,
  QUANTITY_MAX,
  REASON_MAX,
  type Owner,
} from "../api/inventory";
import { useAction } from "../app/useAction";
import { FormDialog } from "./FormDialog";
import { validQuantity, type OwnerOption } from "./owners";

/** Where a new item starts: blank, or carried over from a feed event. */
export interface GiveStart {
  owner: Owner;
  name: string;
  entity_id: string | null;
  source_event_id: string | null;
}

export const BLANK_GIVE: GiveStart = {
  owner: PARTY,
  name: "",
  entity_id: null,
  source_event_id: null,
};

interface GiveItemDialogProps {
  campaignId: string;
  start: GiveStart;
  owners: OwnerOption[];
  onOpenKnowledge: (entityId: string) => void;
  onClose: () => void;
}

/** The DM hands a new item to the party pool or to one character. */
export function GiveItemDialog({
  campaignId,
  start,
  owners,
  onOpenKnowledge,
  onClose,
}: GiveItemDialogProps) {
  // A feed event may address a character who has since left the campaign.
  const [owner, setOwner] = useState<Owner>(
    owners.some((row) => row.value === start.owner) ? start.owner : PARTY,
  );
  const [name, setName] = useState(start.name.slice(0, NAME_MAX));
  const [quantity, setQuantity] = useState<number | null>(1);
  const [notes, setNotes] = useState("");
  const [hidden, setHidden] = useState(false);
  const [reason, setReason] = useState("");
  const give = useAction(giveItem, {
    invalidate: [inventoryKeys.all(campaignId)],
    success: (_item, input) => `Gave ${input.name}.`,
    onSuccess: onClose,
  });
  const trimmed = name.trim();
  const valid =
    trimmed !== "" && validQuantity(quantity, 1) && notes.length <= NOTES_MAX;
  const submit = () => {
    if (!valid || give.isPending) return;
    give.run({
      campaignId,
      owner,
      name: trimmed,
      quantity,
      notes,
      hiddenFromParty: hidden,
      reason,
      entityId: start.entity_id,
      sourceEventId: start.source_event_id,
    });
  };
  return (
    <FormDialog
      title="Give item"
      submitLabel={`Give ${trimmed || "item"}`}
      isPending={give.isPending}
      isInvalid={!valid}
      onSubmit={submit}
      onClose={onClose}
    >
      <Selector
        label="Give to"
        options={owners}
        value={owner}
        onChange={setOwner}
        width="100%"
      />
      <TextInput
        label="Item name"
        value={name}
        onChange={(value) => setName(value.slice(0, NAME_MAX))}
        isRequired
        autoComplete="off"
        width="100%"
      />
      <NumberInput
        label="Quantity"
        value={quantity}
        onChange={setQuantity}
        min={1}
        max={QUANTITY_MAX}
        isIntegerOnly
        isRequired
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
        description="Players cannot move it, and nothing is posted to the table."
        value={hidden}
        onChange={setHidden}
      />
      <TextInput
        label="Reason"
        description="Kept in the item's history."
        value={reason}
        onChange={(value) => setReason(value.slice(0, REASON_MAX))}
        isOptional
        width="100%"
      />
      {start.entity_id && (
        <HStack gap={2} vAlign="center" wrap="wrap">
          <Text type="label" color="secondary">
            Linked knowledge
          </Text>
          <Button
            label={`Open knowledge for ${trimmed || "this item"}`}
            size="sm"
            variant="ghost"
            onClick={() => onOpenKnowledge(start.entity_id!)}
          >
            {trimmed || "Open knowledge"}
          </Button>
        </HStack>
      )}
    </FormDialog>
  );
}
