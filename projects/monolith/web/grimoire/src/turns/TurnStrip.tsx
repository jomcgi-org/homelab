import type { TurnOrder } from "../api/session";

export interface TurnStripProps {
  order: TurnOrder;
  dm: boolean;
}

/** The turn order at a glance: round, entries and whose turn it is. */
export function TurnStrip(_props: TurnStripProps) {
  return null;
}
