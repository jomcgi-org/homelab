import { useState } from "react";
import { Collapsible } from "@astryxdesign/core/Collapsible";
import { VStack } from "@astryxdesign/core/Layout";
import { List, ListItem } from "@astryxdesign/core/List";
import { Text } from "@astryxdesign/core/Text";
import { VisuallyHidden } from "@astryxdesign/core/VisuallyHidden";
import {
  useItemHistory,
  type InventoryChange,
  type InventoryItem,
} from "../api/inventory";
import { Loaded } from "../app/Loaded";

interface ItemHistoryProps {
  campaignId: string;
  item: InventoryItem;
  /** Names an owner recorded in a change; the DM's view carries character ids. */
  ownerName: (owner: string | null) => string;
}

/** The item's audit trail, read only once someone asks for it. */
export function ItemHistory({ campaignId, item, ownerName }: ItemHistoryProps) {
  const [open, setOpen] = useState(false);
  return (
    <Collapsible
      defaultIsOpen={false}
      onOpenChange={setOpen}
      trigger={
        <Text type="label">
          History<VisuallyHidden> for {item.name}</VisuallyHidden>
        </Text>
      }
    >
      {open && (
        <Changes campaignId={campaignId} item={item} ownerName={ownerName} />
      )}
    </Collapsible>
  );
}

function Changes({ campaignId, item, ownerName }: ItemHistoryProps) {
  const history = useItemHistory(campaignId, item.id);
  return (
    <Loaded query={history} what="this item's history">
      {(changes) =>
        changes.length === 0 ? (
          <Text type="body" color="secondary">
            No changes yet.
          </Text>
        ) : (
          <List hasDividers density="compact" edge="flush">
            {changes.map((change) => (
              <ListItem
                key={change.id}
                label={<Change change={change} ownerName={ownerName} />}
              />
            ))}
          </List>
        )
      }
    </Loaded>
  );
}

function Change({
  change,
  ownerName,
}: {
  change: InventoryChange;
  ownerName: (owner: string | null) => string;
}) {
  const owner = change.changes.owner;
  return (
    <VStack gap={0.5}>
      <Text type="body">
        <strong>{change.action}</strong>: delta {change.delta}, quantity after{" "}
        {change.quantity_after}.
        {owner && ` Owner: ${ownerName(owner.from)} to ${ownerName(owner.to)}.`}
      </Text>
      {change.reason && (
        <Text type="body" as="p" wordBreak="break-word">
          {change.reason}
        </Text>
      )}
      <Text type="supporting">
        <time dateTime={change.created_at}>
          {new Date(change.created_at).toLocaleString()}
        </time>
      </Text>
    </VStack>
  );
}
