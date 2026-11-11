export interface SessionNotesPanelProps {
  campaignId: string;
  dm: boolean;
  onShowEvent: (eventId: string) => void;
  onOpenKnowledge: (entityId: string) => void;
}

/** Character and party notes beside the feed, including pinned events. */
export function SessionNotesPanel(_props: SessionNotesPanelProps) {
  return null;
}
