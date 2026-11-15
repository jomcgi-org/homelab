import { Badge } from "@astryxdesign/core/Badge";
import { Button } from "@astryxdesign/core/Button";
import { Card } from "@astryxdesign/core/Card";
import { Heading } from "@astryxdesign/core/Heading";
import { HStack, VStack } from "@astryxdesign/core/Layout";
import { Text } from "@astryxdesign/core/Text";
import { PARTY, type InventoryItem } from "../api/inventory";
import { ItemHistory } from "./ItemHistory";
import { moveLabel } from "./MoveItemDialog";

interface ItemRowProps {
  campaignId: string;
  item: InventoryItem;
  dm: boolean;
  /** False when there is nowhere this viewer may move the item. */
  canMove: boolean;
  ownerName: (owner: string | null) => string;
  onEdit: () => void;
  onMove: () => void;
  onOpenKnowledge: (entityId: string) => void;
}

/** One item: what it is, then the actions this viewer is allowed. */
export function ItemRow({
  campaignId,
  item,
  dm,
  canMove,
  ownerName,
  onEdit,
  onMove,
  onOpenKnowledge,
}: ItemRowProps) {
  const canEdit = dm || item.is_mine;
  return (
    <li>
      <Card>
        <VStack gap={2}>
          <HStack gap={2} vAlign="center" wrap="wrap">
            <Heading level={3}>
              {item.name}{" "}
              <Text type="inherit" color="secondary" hasTabularNumbers>
                × {item.quantity}
              </Text>
            </Heading>
            {item.hidden_from_party && <Badge label="Hidden from party" />}
          </HStack>
          {item.notes && (
            <Text type="body" as="p">
              <span className="inventory-notes">{item.notes}</span>
            </Text>
          )}
          <HStack gap={2} wrap="wrap">
            {item.entity && (
              <Button
                label={`Explore ${item.entity.name} for ${item.name}`}
                size="sm"
                variant="ghost"
                onClick={() => onOpenKnowledge(item.entity!.id)}
              >
                {item.entity.name}
              </Button>
            )}
            {canEdit && (
              <Button
                label={
                  dm ? `Edit ${item.name}` : `Adjust quantity for ${item.name}`
                }
                size="sm"
                onClick={onEdit}
              >
                {dm ? "Edit" : "Adjust quantity"}
              </Button>
            )}
            {canMove && (
              <Button
                label={moveLabel(item, dm)}
                size="sm"
                isDisabled={item.quantity === 0}
                onClick={onMove}
              >
                {dm ? "Move" : item.owner === PARTY ? "Take" : "Move to party"}
              </Button>
            )}
          </HStack>
          <ItemHistory
            campaignId={campaignId}
            item={item}
            ownerName={ownerName}
          />
        </VStack>
      </Card>
    </li>
  );
}
