import type { TableCharacter } from "../screens/session/table";

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

/** Party and character inventory, with the DM's give, move and remove. */
export function InventoryPanel(_props: InventoryPanelProps) {
  return null;
}
