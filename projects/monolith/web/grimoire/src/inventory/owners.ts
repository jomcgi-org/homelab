import { PARTY, QUANTITY_MAX, type Owner } from "../api/inventory";
import type { TableCharacter } from "../screens/session/table";

export interface OwnerOption {
  value: Owner;
  label: string;
}

export const PARTY_POOL = "Party pool";

/** Everywhere an item can live: the pool, then each character the viewer may name. */
export const ownerOptions = (characters: TableCharacter[]): OwnerOption[] => [
  { value: PARTY, label: PARTY_POOL },
  ...characters.map((row) => ({ value: row.id, label: row.character_name })),
];

export const validQuantity = (
  value: number | null,
  min: number,
  max = QUANTITY_MAX,
): value is number =>
  value !== null && Number.isInteger(value) && value >= min && value <= max;
