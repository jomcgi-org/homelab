import type { TurnOrder } from "../api/session";

export interface YourTurnBannerProps {
  order: TurnOrder;
  /** The viewer's own character ids. */
  characterIds: string[];
}

/** Tells a player, visibly and to a screen reader, that it is their turn. */
export function YourTurnBanner(_props: YourTurnBannerProps) {
  return null;
}
