import type { MemberView } from "../api/schema";
import type { SessionEvent, TurnOrder } from "../api/session";
import type { TableCharacter } from "../screens/session/table";

export interface InitiativeEditorProps {
  campaignId: string;
  sessionId: string;
  initiative: TurnOrder;
  characters: TableCharacter[];
  events: SessionEvent[];
  members: MemberView[];
}

/** The DM sets, advances and ends the turn order. */
export function InitiativeEditor(_props: InitiativeEditorProps) {
  return null;
}
