import type { SessionEvent } from "../api/session";

export interface HandoutCardProps {
  campaignId: string;
  sessionId: string;
  handout: SessionEvent;
  dm: boolean;
  /** Pin this handout to the viewer's notes. */
  onPin: (event: SessionEvent) => void;
  onOpenKnowledge: (entityId: string) => void;
}

/** A handout in the feed: title, text, image and linked knowledge. */
export function HandoutCard(_props: HandoutCardProps) {
  return null;
}
