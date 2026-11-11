export interface KnowledgeDrawerProps {
  campaignId: string;
  /** The entity to show, or null when closed. */
  entityId: string | null;
  onClose: () => void;
}

/** A side drawer with the viewer's projection of one entity. */
export function KnowledgeDrawer(_props: KnowledgeDrawerProps) {
  return null;
}
