export interface KnowledgeSearchProps {
  campaignId: string;
  /** Open an entity in the knowledge drawer. */
  onOpenKnowledge: (entityId: string) => void;
  /** Scroll the feed to a session event. */
  onShowEvent: (eventId: string) => void;
}

/** Player search across what their character knows. */
export function KnowledgeSearch(_props: KnowledgeSearchProps) {
  return null;
}
