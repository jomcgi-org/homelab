import { useEffect } from "react";
import { Banner } from "@astryxdesign/core/Banner";
import { useAnnounce } from "@astryxdesign/core/hooks";
import type { TurnOrder } from "../api/session";

export interface YourTurnBannerProps {
  order: TurnOrder;
  /** The viewer's own character ids. */
  characterIds: string[];
}

/**
 * Only an active entry that names one of the viewer's own characters counts:
 * a masked or omitted entry carries no character id.
 */
function isViewersTurn(order: TurnOrder, characterIds: string[]): boolean {
  const index = order?.active_index ?? null;
  const active = index === null ? undefined : order?.entries?.[index];
  const character = active?.player_character_id;
  return (
    Boolean(character) &&
    active?.hidden !== true &&
    characterIds.includes(character!)
  );
}

/** Tells a player, visibly and to a screen reader, that it is their turn. */
export function YourTurnBanner({ order, characterIds }: YourTurnBannerProps) {
  const yours = isViewersTurn(order, characterIds);
  const announce = useAnnounce();
  useEffect(() => {
    if (yours) announce("Your turn");
  }, [yours, announce]);
  if (!yours) return null;
  return <Banner status="info" title="Your turn" collapsible={false} />;
}
