import type { Journal } from "../api/schema";

export type JournalView = "mine" | "party";

export interface JournalPanelProps {
  /** Null while loading or when the session has no journal yet. */
  journal: Journal | null;
  view: JournalView;
  onViewChange: (view: JournalView) => void;
  onShowEvent: (eventId: string) => void;
  onOpenKnowledge: (entityId: string) => void;
}

/** What this viewer learned, received and rolled in the session. */
export function JournalPanel(_props: JournalPanelProps) {
  return null;
}
