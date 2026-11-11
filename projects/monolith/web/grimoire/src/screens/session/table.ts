/** Shapes the session screen and its panels share. */
import type {
  CharacterView,
  LobbyCampaignView,
  LobbyUserView,
} from "../../api/schema";
import type { ApprovedSheet } from "../../api/session";

/** A character with the numbers from its latest approved sheet, if any. */
export interface TableCharacter extends CharacterView {
  approved: ApprovedSheet | null;
}

/** Who is looking: the campaign entry carries their role and character. */
export interface Viewer {
  user: LobbyUserView;
  campaign: LobbyCampaignView;
  dm: boolean;
}
