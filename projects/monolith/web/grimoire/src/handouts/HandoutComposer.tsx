import type { TableCharacter } from "../screens/session/table";

export interface HandoutComposerProps {
  campaignId: string;
  sessionId: string;
  characters: TableCharacter[];
}

/** The DM writes a handout, optionally with an image, for chosen characters. */
export function HandoutComposer(_props: HandoutComposerProps) {
  return null;
}
